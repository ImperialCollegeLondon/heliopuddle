"""The rules data-flow diagram: layout of storage-location boxes, rule curves and labels (UI-04)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from heliopuddle.server.schemas import RuleRead, StorageLocationSummary

NODE_W = 210
NODE_H = 84
COL_GAP = 240
ROW_GAP = 36
PAD_X = 32
PAD_TOP = 60
PAD_BOTTOM = 36
DELETE_STEP = 34
BACK_DIP = 70


@dataclass
class FlowNode:
    """A storage location in the diagram (pixel coordinates of its box)."""

    name: str
    active: bool
    depth: int
    x: int = 0
    y: int = 0
    files: int | None = None
    bytes: int | None = None
    kind: str = ""
    role: str = ""
    deletes: list[RuleRead] = field(default_factory=list)

    @property
    def right(self) -> int:
        """The x of the box's right edge."""
        return self.x + NODE_W

    @property
    def middle(self) -> int:
        """The y of the box's middle."""
        return self.y + NODE_H // 2


@dataclass(frozen=True)
class FlowLabel:
    """A rule's label pill, centred on ``(x, y)``."""

    rule: str
    text: str
    action: str
    enabled: bool
    x: int
    y: int


@dataclass(frozen=True)
class FlowEdge:
    """A drawn connection: an SVG path and the action that colours it."""

    path: str
    action: str
    enabled: bool


@dataclass
class FlowLayout:
    """The laid-out diagram: boxes, curves and labels on a ``width`` by ``height`` canvas."""

    nodes: list[FlowNode]
    edges: list[FlowEdge]
    labels: list[FlowLabel]
    width: int
    height: int


def flow_depths(names: Sequence[str], rules: Sequence[RuleRead]) -> dict[str, int]:
    """Columns of the diagram: each destination one column right of its deepest source.

    A storage location no rule writes to is in column 0. A rule that closes a cycle
    (found by a depth-first walk in config order) does not push its destination
    right; it is drawn as a backward curve instead.
    """
    known = set(names)
    out: dict[str, list[str]] = {n: [] for n in names}
    for rule in rules:
        src, dst = rule.source, rule.destination
        if dst and src in known and dst in known and src != dst and dst not in out[src]:
            out[src].append(dst)
    forward: dict[str, list[str]] = {n: [] for n in names}
    state: dict[str, int] = {}  # 1 on the walk's stack, 2 done
    order: list[str] = []

    def walk(node: str) -> None:
        state[node] = 1
        for nxt in out[node]:
            if state.get(nxt) == 1:
                continue  # closes a cycle: not a forward edge
            forward[node].append(nxt)
            if nxt not in state:
                walk(nxt)
        state[node] = 2
        order.append(node)

    for name in names:
        if name not in state:
            walk(name)
    depth = dict.fromkeys(names, 0)
    for node in reversed(order):  # a topological order of the forward edges
        for nxt in forward[node]:
            depth[nxt] = max(depth[nxt], depth[node] + 1)
    return depth


def _rule_text(rule: RuleRead) -> str:
    text = f"{rule.name} · {rule.action}"
    return text if rule.effective_enabled else f"{text} · off"


def _build_nodes(
    storage_locations: Sequence[StorageLocationSummary | Any],
    rules: Sequence[RuleRead],
    kinds: Mapping[str, str],
) -> dict[str, FlowNode]:
    """One node per storage location, with its depth, role and delete rules."""
    depths = flow_depths([s.name for s in storage_locations], rules)
    nodes = {
        s.name: FlowNode(
            s.name,
            bool(getattr(s, "active", True)),
            depths[s.name],
            files=getattr(s, "files", None),
            bytes=getattr(s, "bytes", None),
            kind=kinds.get(s.name, ""),
        )
        for s in storage_locations
    }
    for rule in rules:
        if rule.destination is None and rule.source in nodes:
            nodes[rule.source].deletes.append(rule)
    sources = {r.source for r in rules if r.destination}
    targets = {r.destination for r in rules if r.destination}
    for node in nodes.values():
        if not node.active:
            node.role = "inactive"
        elif node.name in sources and node.name not in targets:
            node.role = "source"
    return nodes


def _slot(node: FlowNode) -> int:
    """Vertical space a node takes, including the labels of its delete rules."""
    extra = DELETE_STEP * len(node.deletes) + (10 if node.deletes else 0)
    return NODE_H + extra


def _place_columns(nodes: dict[str, FlowNode], names: Sequence[str]) -> int:
    """Set every node's position; return the height of the tallest column."""
    columns: dict[int, list[FlowNode]] = {}
    for name in names:
        columns.setdefault(nodes[name].depth, []).append(nodes[name])
    heights = {
        d: sum(_slot(n) for n in col) + ROW_GAP * (len(col) - 1) for d, col in columns.items()
    }
    tallest = max(heights.values(), default=NODE_H)
    for d, col in columns.items():
        y = PAD_TOP + (tallest - heights[d]) // 2
        for node in col:
            node.x = PAD_X + d * (NODE_W + COL_GAP)
            node.y = y
            y += _slot(node) + ROW_GAP
    return tallest


def _group_rules(
    rules: Sequence[RuleRead], nodes: dict[str, FlowNode]
) -> dict[tuple[str, str], list[RuleRead]]:
    """Copy and move rules grouped by their (source, destination) pair."""
    grouped: dict[tuple[str, str], list[RuleRead]] = {}
    for rule in rules:
        if rule.destination and rule.source in nodes and rule.destination in nodes:
            grouped.setdefault((rule.source, rule.destination), []).append(rule)
    return grouped


def _curve(src: FlowNode, dst: FlowNode) -> tuple[str, int, int, int | None]:
    """The SVG path between two nodes, its label centre and the dip bottom (if any)."""
    if dst.depth > src.depth:
        x1, y1, x2, y2 = src.right, src.middle, dst.x, dst.middle
        dx = (x2 - x1) // 2
        path = f"M{x1} {y1} C{x1 + dx} {y1}, {x2 - dx} {y2}, {x2 - 8} {y2}"
        return path, (x1 + x2) // 2, (y1 + y2) // 2, None
    x1, x2 = src.x + NODE_W // 2 + 20, dst.x + NODE_W // 2 - 20
    y1 = src.y + _slot(src)
    y2 = dst.y + _slot(dst) + 8
    low = max(y1, y2) + BACK_DIP
    path = f"M{x1} {y1} C{x1} {low}, {x2} {low}, {x2} {y2}"
    return path, (x1 + x2) // 2, low - 18, low


def _label(rule: RuleRead, x: int, y: int) -> FlowLabel:
    return FlowLabel(rule.name, _rule_text(rule), rule.action, rule.effective_enabled, x, y)


def _rule_edges(
    grouped: dict[tuple[str, str], list[RuleRead]],
    nodes: dict[str, FlowNode],
    edges: list[FlowEdge],
    labels: list[FlowLabel],
    bottom: int,
) -> int:
    """Add a curve and stacked labels per rule group; return the new diagram bottom."""
    for (src_name, dst_name), group in grouped.items():
        path, cx, cy, low = _curve(nodes[src_name], nodes[dst_name])
        if low is not None:
            bottom = max(bottom, low + 20)
        lead = next((r for r in group if r.effective_enabled), group[0])
        edges.append(FlowEdge(path, lead.action, any(r.effective_enabled for r in group)))
        start = cy - (len(group) - 1) * 15
        labels.extend(_label(rule, cx, start + 30 * i) for i, rule in enumerate(group))
    return bottom


def _delete_marks(
    nodes: dict[str, FlowNode], edges: list[FlowEdge], labels: list[FlowLabel]
) -> None:
    """Hang each node's delete rules as labels under it, with a short line."""
    for node in nodes.values():
        for i, rule in enumerate(node.deletes):
            y = node.y + NODE_H + 10 + DELETE_STEP * i + DELETE_STEP // 2
            labels.append(_label(rule, node.x + NODE_W // 2, y))
        if node.deletes:
            top = node.y + NODE_H
            end = node.y + NODE_H + 10 + DELETE_STEP * (len(node.deletes) - 1) + 6
            x = node.x + 22
            edges.append(
                FlowEdge(
                    f"M{x} {top} L{x} {end}",
                    "delete",
                    any(r.effective_enabled for r in node.deletes),
                )
            )


def layout_flow(
    storage_locations: Sequence[StorageLocationSummary | Any],
    rules: Sequence[RuleRead],
    kinds: Mapping[str, str] | None = None,
) -> FlowLayout:
    """Lay out the rules data-flow diagram by flow depth (UI-04).

    Storage locations are boxes in columns by :func:`flow_depths`, stacked in config
    order within a column and centred vertically. A copy or move rule is a curve
    from its source to its destination (to the right; a rule pointing back to a
    shallower column dips under the boxes); rules between the same pair share a
    curve and stack their labels. A delete rule hangs as a label under its source.

    Args:
        storage_locations: objects with ``name`` and ``active`` (and optionally
            ``files`` and ``bytes``), in config order.
        rules: the project's rules.
        kinds: storage-location name to a short backend label.

    Returns:
        The layout, in pixels.
    """
    kinds = kinds or {}
    names = [s.name for s in storage_locations]
    nodes = _build_nodes(storage_locations, rules, kinds)
    tallest = _place_columns(nodes, names)
    edges: list[FlowEdge] = []
    labels: list[FlowLabel] = []
    bottom = _rule_edges(_group_rules(rules, nodes), nodes, edges, labels, PAD_TOP + tallest)
    _delete_marks(nodes, edges, labels)
    depth_count = max((n.depth for n in nodes.values()), default=0) + 1
    width = 2 * PAD_X + depth_count * NODE_W + (depth_count - 1) * COL_GAP
    height = max(bottom, PAD_TOP + tallest) + PAD_BOTTOM
    ordered = [nodes[n] for n in names]
    return FlowLayout(ordered, edges, labels, width, height)

"""rclone config sources and the session's merged temp config (STO-20).

A storage location's ``storage_options`` name exactly one config source:
``config_block`` (a stored block, read and written through the registered
block store) or ``config_file`` (an ``rclone.conf`` on this machine). A
session concatenates the configs it needs into one private temp config and,
when it ends, writes each config whose sections rclone changed (a refreshed
OAuth token) back to where it came from.
"""

from __future__ import annotations

import configparser
import contextlib
import hashlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from heliopuddle.storage.rclone.errors import RcloneConfigError, RcloneUnavailable

__all__ = [
    "BlockStore",
    "ConfigSource",
    "Sections",
    "changed_configs",
    "load_config",
    "merge_configs",
    "parse_sections",
    "save_config",
    "source_from_options",
]

Sections = dict[str, dict[str, str]]

VERIFY_MODES = ("auto", "native", "content", "size")


class BlockStore(Protocol):
    """Where ``config_block`` configs live (the server registers Prefect blocks)."""

    def load(self, name: str) -> str:
        """Return the rclone config text of block ``name``; raise ``LookupError`` if missing."""
        ...

    def save(self, name: str, contents: str) -> None:
        """Replace the rclone config text of block ``name``."""
        ...


@dataclass(frozen=True)
class ConfigSource:
    """One rclone config: a block name or an absolute file path.

    Attributes:
        kind: ``"block"`` or ``"file"``.
        name: the block name, or the absolute path of the config file.
    """

    kind: Literal["block", "file"]
    name: str

    @property
    def lock_name(self) -> str:
        """The name of this config's lock: ``rclone:<block>`` or ``rclone:file:<sha256[:12]>``.

        Lock (concurrency limit) names cannot contain ``/``, so a file is named
        by a hash of its absolute path.
        """
        if self.kind == "block":
            return f"rclone:{self.name}"
        return f"rclone:file:{hashlib.sha256(self.name.encode()).hexdigest()[:12]}"

    def describe(self) -> str:
        """A human-readable name for messages (never the contents)."""
        return (
            f"config block {self.name!r}" if self.kind == "block" else f"config file {self.name!r}"
        )


def source_from_options(options: Mapping[str, Any]) -> ConfigSource:
    """Return the config source named by an rclone storage location's ``storage_options``.

    Raises:
        RcloneConfigError: not exactly one of ``config_block`` and ``config_file``.
    """
    block = options.get("config_block")
    path = options.get("config_file")
    if bool(block) == bool(path):
        raise RcloneConfigError(
            "an rclone storage location needs exactly one of storage_options "
            "config_block and config_file"
        )
    if block:
        return ConfigSource("block", str(block))
    return ConfigSource("file", str(Path(str(path)).expanduser().absolute()))


def load_config(source: ConfigSource, store: BlockStore | None) -> str:
    """Read the config text of ``source``.

    Raises:
        RcloneUnavailable: the block or file does not exist, or no block store
            is registered for a ``config_block``.
    """
    if source.kind == "file":
        try:
            return Path(source.name).read_text()
        except FileNotFoundError:
            raise RcloneUnavailable(f"rclone {source.describe()} does not exist") from None
    if store is None:
        raise RcloneUnavailable(
            f"rclone {source.describe()} cannot be read: no config block store is registered"
        )
    try:
        return store.load(source.name)
    except LookupError:
        raise RcloneUnavailable(f"rclone {source.describe()} does not exist") from None


def save_config(source: ConfigSource, store: BlockStore | None, text: str) -> None:
    """Write ``text`` back to ``source`` (a file is replaced atomically, keeping its mode)."""
    if source.kind == "block":
        if store is None:  # load_config needed one, so this cannot happen
            raise RcloneUnavailable(f"rclone {source.describe()}: no block store")
        store.save(source.name, text)
        return
    target = Path(source.name)
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    fd, tmp = tempfile.mkstemp(prefix=".hp-rclone-", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def parse_sections(text: str, where: str = "config") -> Sections:
    """Parse rclone config text into ``{remote: {key: value}}`` (keys keep their case).

    Raises:
        RcloneConfigError: the text is not a valid config (the message never
            quotes its contents).
    """
    parser = configparser.ConfigParser(
        interpolation=None, default_section="\x00hp-none", strict=True
    )
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_string(text)
    except configparser.DuplicateSectionError as e:
        raise RcloneConfigError(f"rclone {where} defines remote {e.section!r} twice") from None
    except configparser.Error as e:
        raise RcloneConfigError(
            f"rclone {where} is not a valid config ({type(e).__name__})"
        ) from None
    return {name: dict(parser[name]) for name in parser.sections()}


def merge_configs(texts: Mapping[ConfigSource, str]) -> tuple[str, dict[ConfigSource, Sections]]:
    """Concatenate configs into one, checking that no remote is defined twice.

    Returns:
        The merged config text and each source's parsed sections.

    Raises:
        RcloneConfigError: a remote name is defined by more than one config.
    """
    owners: dict[str, ConfigSource] = {}
    parsed: dict[ConfigSource, Sections] = {}
    for source, text in texts.items():
        sections = parse_sections(text, source.describe())
        for name in sections:
            if name in owners:
                raise RcloneConfigError(
                    f"rclone remote {name!r} is defined by both {owners[name].describe()} "
                    f"and {source.describe()}"
                )
            owners[name] = source
        parsed[source] = sections
    merged = "\n".join(text.rstrip("\n") + "\n" for text in texts.values())
    return merged, parsed


def _render(sections: Sections) -> str:
    blocks = []
    for name, values in sections.items():
        lines = [f"[{name}]", *(f"{k} = {v}" for k, v in values.items())]
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


def changed_configs(
    originals: Mapping[ConfigSource, Sections], final_text: str
) -> dict[ConfigSource, str]:
    """Return the new text of every config whose sections rclone changed in ``final_text``.

    A remote that vanished from the temp config keeps its original section.
    """
    final = parse_sections(final_text, "session config")
    changed: dict[ConfigSource, str] = {}
    for source, sections in originals.items():
        updated = {name: final.get(name, values) for name, values in sections.items()}
        if updated != sections:
            changed[source] = _render(updated)
    return changed

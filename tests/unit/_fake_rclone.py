"""A stand-in ``rclone`` for unit tests: ``rclone rcd`` over local directories.

Run as a script (stdlib only). It implements the rc endpoints hp calls, with
basic auth from ``RCLONE_RC_USER``/``RCLONE_RC_PASS``, on the Unix socket
given by ``--rc-addr unix://...``, and ``--rc-serve`` ranged reads. Remotes in
the config are ``type = alias`` with ``remote = <dir>`` and these test knobs:

- ``hashes = md5,sha1``: hash types the remote reports (default none);
- ``about_free = <n>``: free bytes ``operations/about`` reports (default: unsupported);
- ``refresh = true``: at start, write ``token = refreshed`` into the config
  (as rclone does when it refreshes an OAuth token);
- ``corrupt = flip|grow``: ``copyfile`` to this remote stores a changed last byte
  (same size) or one byte more.
"""

from __future__ import annotations

import base64
import configparser
import hashlib
import json
import os
import shutil
import socketserver
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote

HASHERS = {"md5": hashlib.md5, "sha1": hashlib.sha1, "sha256": hashlib.sha256}


class RcError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _arg(args: list[str], name: str) -> str:
    return args[args.index(name) + 1]


class Remotes:
    def __init__(self, config_path: str) -> None:
        self.path = config_path
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # type: ignore[assignment,method-assign]
        parser.read(config_path)
        self.sections = {name: dict(parser[name]) for name in parser.sections()}
        if any(s.get("refresh") == "true" for s in self.sections.values()):
            for section in self.sections.values():
                if section.get("refresh") == "true":
                    section["token"] = "refreshed"
            with open(config_path, "w") as f:
                for name, values in self.sections.items():
                    f.write(
                        f"[{name}]\n" + "".join(f"{k} = {v}\n" for k, v in values.items()) + "\n"
                    )

    def section(self, fs: str) -> dict[str, str]:
        name = fs.split(":", 1)[0]
        if name not in self.sections:
            raise RcError(500, f'didn\'t find section in config file ("{name}")')
        return self.sections[name]

    def resolve(self, fs: str, remote: str = "") -> Path:
        if fs.startswith("/"):
            return Path(fs) / remote if remote else Path(fs)
        _name, _, sub = fs.partition(":")
        base = Path(self.section(fs)["remote"])
        return base / sub / remote if remote else base / sub


def _item(path: Path, rel: str, hashes: list[str] | None) -> dict[str, Any]:
    st = path.stat()
    mtime = datetime.fromtimestamp(st.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"
    item: dict[str, Any] = {
        "Path": rel,
        "Name": path.name,
        "Size": -1 if path.is_dir() else st.st_size,
        "ModTime": mtime,
        "IsDir": path.is_dir(),
    }
    if hashes is not None and path.is_file():
        data = path.read_bytes()
        item["Hashes"] = {h: HASHERS[h](data).hexdigest() for h in hashes}
    return item


def _looks_same(src: Path, dst: Path) -> bool:
    """rclone's quick check: the same size and modification time (1s precision)."""
    if not dst.is_file():
        return False
    a, b = src.stat(), dst.stat()
    return a.st_size == b.st_size and abs(a.st_mtime - b.st_mtime) < 1


class Rc:
    def __init__(self, remotes: Remotes) -> None:
        self.remotes = remotes

    def _hash_types(self, fs: str, opt: dict[str, Any]) -> list[str] | None:
        if not opt.get("showHash"):
            return None
        supported = [h for h in self.remotes.section(fs).get("hashes", "").split(",") if h]
        wanted = opt.get("hashTypes") or supported
        return [h for h in supported if h in wanted]

    def call(self, endpoint: str, p: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, endpoint.replace("/", "_"), None)
        if handler is None:
            raise RcError(404, f"couldn't find method {endpoint!r}")
        return handler(p)

    def answer(self, endpoint: str, body: bytes) -> tuple[int, bytes]:
        """The HTTP status and JSON body for a POST to ``endpoint``."""
        try:
            result, status = self.call(endpoint, json.loads(body or b"{}")), 200
        except RcError as e:
            result, status = {"error": str(e), "status": e.status, "path": endpoint}, e.status
        return status, json.dumps(result).encode()

    def core_version(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"version": "v1.76.0-fake"}

    def config_listremotes(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"remotes": list(self.remotes.sections)}

    def operations_list(self, p: dict[str, Any]) -> dict[str, Any]:
        rel = p.get("remote", "")
        path = self.remotes.resolve(p["fs"], rel)
        if not path.is_dir():
            if path.exists():
                raise RcError(500, "error in ListJSON: not a directory")
            raise RcError(404, "error in ListJSON: directory not found")
        items = [_item(c, f"{rel}/{c.name}".lstrip("/"), None) for c in sorted(path.iterdir())]
        return {"list": items}

    def operations_stat(self, p: dict[str, Any]) -> dict[str, Any]:
        rel = p.get("remote", "")
        path = self.remotes.resolve(p["fs"], rel)
        if not path.exists():
            return {"item": None}
        return {"item": _item(path, rel, self._hash_types(p["fs"], p.get("opt") or {}))}

    def operations_about(self, p: dict[str, Any]) -> dict[str, Any]:
        free = self.remotes.section(p["fs"]).get("about_free")
        if free is None:
            raise RcError(500, f"{p['fs']} doesn't support about")
        return {"free": int(free), "total": int(free) * 2}

    def operations_hashsum(self, p: dict[str, Any]) -> dict[str, Any]:
        path = Path(p["fs"])
        if not path.is_file():
            raise RcError(500, "fake: hashsum only hashes one file")
        digest = HASHERS[p["hashType"]](path.read_bytes()).hexdigest()
        return {"hashType": p["hashType"], "hashsum": [f"{digest}  {path.name}"]}

    def _transfer(self, p: dict[str, Any], move: bool) -> dict[str, Any]:
        src = self.remotes.resolve(p["srcFs"], p["srcRemote"])
        dst = self.remotes.resolve(p["dstFs"], p["dstRemote"])
        if not src.is_file():
            raise RcError(404, "object not found")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if _looks_same(src, dst) and not (p.get("_config") or {}).get("IgnoreTimes"):
            if move:  # as rclone does: "already there", so the source is just deleted
                src.unlink()
            return {}
        if move:
            os.replace(src, dst)
        else:
            shutil.copyfile(src, dst)
        corrupt = (
            "" if p["dstFs"].startswith("/") else self.remotes.section(p["dstFs"]).get("corrupt")
        )
        if corrupt == "grow":
            dst.write_bytes(dst.read_bytes() + b"!")
        elif corrupt == "flip":
            dst.write_bytes(dst.read_bytes()[:-1] + b"!")
        return {}

    def operations_copyfile(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._transfer(p, move=False)

    def operations_movefile(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._transfer(p, move=True)

    def operations_deletefile(self, p: dict[str, Any]) -> dict[str, Any]:
        path = self.remotes.resolve(p["fs"], p["remote"])
        if not path.is_file():
            raise RcError(404, "object not found")
        path.unlink()
        return {}

    def operations_mkdir(self, p: dict[str, Any]) -> dict[str, Any]:
        self.remotes.resolve(p["fs"], p.get("remote", "")).mkdir(parents=True, exist_ok=True)
        return {}

    def operations_rmdir(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            self.remotes.resolve(p["fs"], p["remote"]).rmdir()
        except OSError as e:
            raise RcError(500, f"remove: {(e.strerror or '').lower()}") from None
        return {}

    def operations_purge(self, p: dict[str, Any]) -> dict[str, Any]:
        shutil.rmtree(self.remotes.resolve(p["fs"], p["remote"]))
        return {}


def serve_file(rc: Rc, url_path: str, ranged: str | None) -> tuple[int, bytes]:
    """``--rc-serve``: ``GET /[remote:path]/file`` with an optional ``Range``."""
    spec, _, rel = url_path.lstrip("/").partition("]/")
    try:
        path = rc.remotes.resolve(spec.lstrip("["), rel)
    except RcError as e:
        return e.status, str(e).encode()
    if not path.is_file():
        return 404, b'{"error": "object not found"}'
    data = path.read_bytes()
    if not ranged:
        return 200, data
    start, _, end = ranged.removeprefix("bytes=").partition("-")
    return 206, data[int(start) : int(end) + 1 if end else None]


def make_handler(rc: Rc, auth: str, server_box: list[Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def address_string(self) -> str:
            return "unix"

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            if self.headers.get("Authorization") == auth:
                return True
            self._send(401, b'{"error": "unauthorized"}')
            return False

        def do_POST(self) -> None:
            if not self._authorised():
                return
            endpoint = self.path.strip("/")
            length = int(self.headers.get("Content-Length") or 0)
            self._send(*rc.answer(endpoint, self.rfile.read(length)))
            if endpoint == "core/quit":
                threading.Thread(target=server_box[0].shutdown).start()

        def do_GET(self) -> None:
            if self._authorised():
                self._send(*serve_file(rc, unquote(self.path), self.headers.get("Range")))

    return Handler


def main(args: list[str]) -> int:
    if args[:1] == ["version"]:
        print("rclone v1.76.0-fake")
        return 0
    if args[:1] != ["rcd"]:
        print(f"fake rclone: unsupported {args}", file=sys.stderr)
        return 2
    user, password = os.environ.get("RCLONE_RC_USER"), os.environ.get("RCLONE_RC_PASS")
    if not user or not password:
        print("fake rclone: refusing to run without auth", file=sys.stderr)
        return 3
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    rc = Rc(Remotes(_arg(args, "--config")))
    sock = _arg(args, "--rc-addr").removeprefix("unix://")
    box: list[Any] = []
    server = socketserver.ThreadingUnixStreamServer(sock, make_handler(rc, f"Basic {token}", box))
    server.daemon_threads = True
    box.append(server)
    server.serve_forever(poll_interval=0.05)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

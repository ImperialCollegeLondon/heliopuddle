"""``heliopuddle file ...`` through Typer's runner against an in-process API (CLI-15)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from heliopuddle.cli import app
from heliopuddle.cli_file import session as cli_file_session
from heliopuddle.client import FileRecord, HelioPuddleClient
from heliopuddle.server.api import create_api
from heliopuddle.server.context import AppContext
from heliopuddle.server.database.orm import FileStorageLocation, StorageLocation
from heliopuddle.server.schemas import FileState

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HELIOPUDDLE_API_URL",
        "HELIOPUDDLE_PROJECT",
        "HELIOPUDDLE_TOKEN",
        "HELIOPUDDLE_USERNAME",
        "HELIOPUDDLE_PASSWORD",
        "HELIOPUDDLE_USER",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def api(ctx: AppContext, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict]]:
    """Serve the API in process; the CLI's client factory talks to it."""
    opened: list[dict] = []
    with TestClient(create_api(ctx)) as http:

        def factory(api_url: str, project: str, **kwargs: str | None) -> HelioPuddleClient:
            opened.append({"api_url": api_url, "project": project, **kwargs})
            return HelioPuddleClient(
                api_url,
                project,
                http_client=http,
                **kwargs,  # type: ignore[arg-type]
            )

        monkeypatch.setattr(cli_file_session, "_client_factory", factory)
        yield opened


def run(*args: str, input: str | None = None):  # type: ignore[no-untyped-def]
    return runner.invoke(app, ["file", *args, "--project", "demo", "--user", "alice"], input=input)


def _local(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / "local" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.req("CLI-15")
def test_add_show_list_download_remove(api: list[dict], tmp_path: Path, store: Path) -> None:
    source = _local(tmp_path, "a.csv", b"x,y\n")

    added = run("add", str(source), "data/a.csv", "--metadata", '{"k": 1}', "--json")
    assert added.exit_code == 0, added.output
    record = json.loads(added.output)
    assert (record["path"], record["metadata"], record["created_by"]) == (
        "data/a.csv",
        {"k": 1},
        "alice",
    )
    assert (store / "data/a.csv").read_bytes() == b"x,y\n"
    assert api[0]["project"] == "demo" and api[0]["user"] == "alice"

    shown = run("show", "--path", "data/a.csv")
    assert shown.exit_code == 0, shown.output
    assert "md5:           " + hashlib.md5(b"x,y\n").hexdigest() in shown.output
    assert "primary: available" in shown.output
    assert run("show", str(record["id"]), "--json").exit_code == 0

    listed = run("list", "data/*")
    assert listed.exit_code == 0
    assert "data/a.csv" in listed.output and "available" in listed.output

    dest = tmp_path / "out"
    dest.mkdir()
    downloaded = run("download", "--path", "data/a.csv", "--to", str(dest))
    assert downloaded.exit_code == 0, downloaded.output
    assert (dest / "a.csv").read_bytes() == b"x,y\n"

    removed = run("remove", str(record["id"]), "--yes")
    assert removed.exit_code == 0, removed.output
    assert not (store / "data/a.csv").exists()
    assert "No files." in run("list").output
    assert "data/a.csv" in run("list", "--deleted").output


@pytest.mark.req("CLI-15")
def test_add_defaults_to_the_file_name_and_conflicts_need_overwrite(
    api: list[dict], tmp_path: Path, store: Path
) -> None:
    assert run("add", str(_local(tmp_path, "r.txt", b"one"))).exit_code == 0
    assert (store / "r.txt").read_bytes() == b"one"

    changed = _local(tmp_path, "other/r.txt", b"two")
    conflict = run("add", str(changed), "r.txt")
    assert conflict.exit_code == 1
    assert "--overwrite" in conflict.output

    assert run("add", str(changed), "r.txt", "--overwrite").exit_code == 0
    assert (store / "r.txt").read_bytes() == b"two"


@pytest.mark.req("CLI-15")
def test_add_a_directory_recursively(api: list[dict], tmp_path: Path, store: Path) -> None:
    folder = tmp_path / "batch"
    _local(folder, "a.txt", b"a")
    _local(folder, "sub/b.txt", b"b")

    refused = run("add", str(folder / "local"))
    assert refused.exit_code == 2
    assert "--recursive" in refused.output

    result = run("add", str(folder / "local"), "in", "-r", "--json")
    assert result.exit_code == 0, result.output
    assert [r["path"] for r in json.loads(result.output)] == ["in/a.txt", "in/sub/b.txt"]
    assert (store / "in/sub/b.txt").read_bytes() == b"b"


@pytest.mark.req("CLI-15")
def test_register_an_object_already_in_the_store(api: list[dict], store: Path) -> None:
    (store / "ext").mkdir()
    (store / "ext/e.bin").write_bytes(b"external")
    digest = hashlib.md5(b"external").hexdigest()

    result = run("register", "ext/e.bin", "--hash", digest, "--size", "8")
    assert result.exit_code == 0, result.output
    assert "primary=available\text/e.bin" in result.output


@pytest.mark.req("CLI-15")
def test_remove_asks_first_and_reports_errors(api: list[dict], tmp_path: Path) -> None:
    run("add", str(_local(tmp_path, "k.txt", b"k")))

    aborted = run("remove", "--path", "k.txt", input="n\n")
    assert aborted.exit_code == 1
    assert "k.txt" in run("list").output

    confirmed = run("remove", "--path", "k.txt", input="y\n")
    assert confirmed.exit_code == 0, confirmed.output

    missing = run("remove", "--path", "k.txt", "--yes")
    assert missing.exit_code == 1
    assert "not found" in missing.output


@pytest.mark.req("CLI-15")
def test_option_errors(api: list[dict], tmp_path: Path) -> None:
    source = str(_local(tmp_path, "o.txt", b"o"))
    assert run("add", source, "--metadata", "[1]").exit_code == 2
    assert run("add", source, "--metadata", "{bad").exit_code == 2
    assert run("add", str(tmp_path / "missing.txt")).exit_code == 2
    bad_path = run("add", source, "../escape.txt")
    assert bad_path.exit_code == 1
    both = runner.invoke(
        app, ["file", "list", "--token", "t", "--username", "u", "--password-stdin"]
    )
    assert both.exit_code == 2


def _id(result) -> int:  # type: ignore[no-untyped-def]
    assert result.exit_code == 0, result.output
    return int(json.loads(result.output)["id"])


@pytest.mark.req("CLI-15")
def test_a_number_is_an_id_and_a_numeric_path_needs_path(
    api: list[dict], tmp_path: Path, store: Path
) -> None:
    first = _id(run("add", str(_local(tmp_path, "f", b"first")), "first.txt", "--json"))
    numeric = _id(run("add", str(_local(tmp_path, "n", b"numeric")), str(first), "--json"))
    assert numeric != first

    # The number is file id `first`, never the file whose path is that number.
    shown = run("show", str(first), "--json")
    assert json.loads(shown.output)["path"] == "first.txt"
    by_path = run("show", "--path", str(first), "--json")
    assert json.loads(by_path.output)["id"] == numeric

    assert run("remove", str(first), "--yes").exit_code == 0
    assert (store / str(first)).read_bytes() == b"numeric"  # untouched
    assert not (store / "first.txt").exists()


@pytest.mark.req("CLI-15")
def test_path_and_name_globs_choose_one_or_more_files(
    api: list[dict], tmp_path: Path, store: Path
) -> None:
    for rel in ("a/x.csv", "a/y.csv", "b/x.csv", "b/z.txt"):
        assert run("add", str(_local(tmp_path, rel, rel.encode())), rel).exit_code == 0

    many = run("show", "--path", "a/*.csv")
    assert many.exit_code == 1
    assert "2 files match" in many.output
    assert run("show", "--path", "a/x.csv").exit_code == 0
    assert run("show", "1", "--path", "a/x.csv").exit_code == 2
    assert "no live file matches" in run("show", "--name", "nope.*").output
    assert run("show", "--name", "a/x.csv").exit_code == 2
    assert run("show").exit_code == 2

    removed = run("remove", "--name", "x.csv", "--yes", "--json")
    assert removed.exit_code == 0, removed.output
    assert sorted(r["path"] for r in json.loads(removed.output)) == ["a/x.csv", "b/x.csv"]
    assert sorted(p.relative_to(store).as_posix() for p in store.rglob("*") if p.is_file()) == [
        "a/y.csv",
        "b/z.txt",
    ]


@pytest.mark.req("CLI-15")
def test_remove_lists_the_files_before_asking(api: list[dict], tmp_path: Path) -> None:
    run("add", str(_local(tmp_path, "q.txt", b"q")))
    asked = run("remove", "--path", "q.txt", input="n\n")
    assert "q.txt" in asked.output and "Delete these 1 file(s)" in asked.output


@pytest.mark.req("CLI-15")
def test_password_stdin_is_read_before_the_confirmation(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    passwords: list[str | None] = []
    with TestClient(create_api(ctx)) as http:

        def factory(api_url: str, project: str, **kwargs: str | None) -> HelioPuddleClient:
            passwords.append(kwargs["password"])
            return HelioPuddleClient(api_url, project, user="alice", http_client=http)

        monkeypatch.setattr(cli_file_session, "_client_factory", factory)
        run("add", str(_local(tmp_path, "p.txt", b"p")))
        result = runner.invoke(
            app,
            [
                "file",
                "remove",
                "--path",
                "p.txt",
                "--project",
                "demo",
                "--username",
                "bob",
                "--password-stdin",
            ],
            input="s3cret\ny\n",
        )
    assert result.exit_code == 0, result.output
    assert passwords[-1] == "s3cret"


@pytest.mark.req("CLI-15")
def test_an_incomplete_delete_says_why_by_copy_state(
    api: list[dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = json.loads(run("add", str(_local(tmp_path, "i.txt", b"i")), "--json").output)
    states = iter(["deleting", "unknown"])

    def remove_file(self: HelioPuddleClient, path_or_id: str | int) -> FileRecord:
        live = self.get_file(int(path_or_id))
        copies = [c.model_copy(update={"state": next(states)}) for c in live.storage_locations]
        return live.model_copy(update={"storage_locations": copies})

    monkeypatch.setattr(HelioPuddleClient, "remove_file", remove_file)
    failed = run("remove", str(record["id"]), "--yes")
    assert failed.exit_code == 1
    assert "delete failed in primary (the index retries)" in failed.output
    changed = run("remove", str(record["id"]), "--yes")
    assert changed.exit_code == 1
    assert "changed content kept in primary" in changed.output
    assert "index retries" not in changed.output


@pytest.mark.req("CLI-15", "CLI-16")
def test_update_metadata_and_content_date(api: list[dict], tmp_path: Path) -> None:
    file_id = _id(
        run("add", str(_local(tmp_path, "u.txt", b"u")), "--metadata", '{"a": 1, "b": 2}', "--json")
    )

    merged = run(
        "update", str(file_id), "--set", "c=3", "--set", "d=text", "--unset", "a", "--json"
    )
    assert merged.exit_code == 0, merged.output
    assert json.loads(merged.output)["metadata"] == {"b": 2, "c": 3, "d": "text"}

    replaced = run("update", "--path", "u.txt", "--metadata", '{"z": [1]}', "--replace-metadata")
    assert replaced.exit_code == 0, replaced.output
    dated = run("update", str(file_id), "--content-date", "2024-01-02T03:04:05", "--json")
    record = json.loads(dated.output)
    assert record["metadata"] == {"z": [1]}
    assert record["content_date"] == "2024-01-02T03:04:05Z"
    cleared = run("update", "--name", "u.txt", "--clear-content-date", "--json")
    assert json.loads(cleared.output)[0]["content_date"] is None

    assert run("update", str(file_id)).exit_code == 2
    assert run("update", str(file_id), "--set", "novalue").exit_code == 2
    assert run("update", str(file_id), "--replace-metadata").exit_code == 2
    both = run("update", str(file_id), "--content-date", "2024-01-01", "--clear-content-date")
    assert both.exit_code == 2


@pytest.mark.req("CLI-15", "CLI-02")
def test_add_with_a_version_number_and_show_it(
    api: list[dict], tmp_path: Path, store: Path
) -> None:
    source = _local(tmp_path, "v.csv", b"v")
    added = run("add", str(source), "v/a.csv", "--version-number", "12", "--json")
    assert added.exit_code == 0, added.output
    record = json.loads(added.output)
    assert (record["version_number"], record["version_text"]) == (12, None)

    shown = run("show", str(record["id"]))
    assert "version:       v12" in shown.output

    same = run("add", str(source), "v/a.csv", "--version-number", "12")
    assert same.exit_code == 0, same.output
    changed = run("add", str(source), "v/a.csv", "--version-number", "13")
    assert changed.exit_code == 1
    assert "has version v12" in changed.output and "cannot be changed" in changed.output
    assert "--overwrite" not in changed.output


@pytest.mark.req("CLI-15", "CLI-13")
def test_register_with_a_version_text_and_show_it(api: list[dict], store: Path) -> None:
    (store / "ext").mkdir()
    (store / "ext/t.bin").write_bytes(b"text")
    digest = hashlib.md5(b"text").hexdigest()

    result = run(
        "register", "ext/t.bin", "--hash", digest, "--size", "4", "--version-text", "rc-1", "--json"
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert (record["version_number"], record["version_text"]) == (None, "rc-1")
    assert "version:       rc-1\n" in run("show", "--path", "ext/t.bin").output


@pytest.mark.req("CLI-15")
def test_show_an_unversioned_file(api: list[dict], tmp_path: Path) -> None:
    run("add", str(_local(tmp_path, "u.txt", b"u")))
    assert "version:       -\n" in run("show", "--path", "u.txt").output


@pytest.mark.req("CLI-15")
def test_version_option_errors(api: list[dict], tmp_path: Path, store: Path) -> None:
    source = str(_local(tmp_path, "o.txt", b"o"))
    both = run("add", source, "--version-number", "1", "--version-text", "one")
    assert both.exit_code == 2
    assert "not both" in both.output
    both_register = run(
        "register", "o.txt", "--hash", "0" * 32, "--size", "1",
        "--version-number", "1", "--version-text", "one",
    )  # fmt: skip
    assert both_register.exit_code == 2
    folder = tmp_path / "local"
    for args in (
        [str(folder), "in", "-r", "--version-number", "1"],
        [str(folder), "in", "--version-text", "x"],
        [source, "--version-number", "1", "-r"],
    ):
        refused = run("add", *args)
        assert refused.exit_code == 2, args
        assert "single file" in refused.output
    assert run("add", source, "--version-number", "-1").exit_code == 2
    # Out of the API's range: refused before any request, exit 2 with the option.
    for command in (["add", source], ["register", "o.txt", "--hash", "0" * 32, "--size", "1"]):
        for option, value in (
            ("--version-number", str(2**63)),
            ("--version-text", ""),
            ("--version-text", "x" * 256),
        ):
            bad = run(*command, option, value)
            assert bad.exit_code == 2, (command, option, bad.output)
            assert f"invalid {option}" in bad.output
    assert not (store / "o.txt").exists() and not (store / "in").exists()


def _moved_to_secondary(ctx: AppContext, tmp_path: Path, store2: Path) -> None:
    """Add data/m.csv on primary, copy it to secondary and delete the primary copy (a move)."""
    data = b"moved\n"
    assert run("add", str(_local(tmp_path, "m.csv", data)), "data/m.csv").exit_code == 0
    (store2 / "data").mkdir(parents=True, exist_ok=True)
    (store2 / "data/m.csv").write_bytes(data)
    digest = hashlib.md5(data).hexdigest()
    registered = run(
        "register", "data/m.csv", "--hash", digest, "--size", "6",
        "--storage-location", "secondary",
    )  # fmt: skip
    assert registered.exit_code == 0, registered.output
    with ctx.write_session() as session:
        copy = session.scalars(
            select(FileStorageLocation)
            .join(StorageLocation)
            .where(StorageLocation.name == "primary")
        ).one()
        copy.state = FileState.DELETED


@pytest.mark.req("CLI-15")
def test_list_shows_a_state_column_per_storage_location(
    api: list[dict], ctx: AppContext, tmp_path: Path
) -> None:
    _moved_to_secondary(ctx, tmp_path, tmp_path / "store2")

    lines = run("list").output.splitlines()

    assert lines[0].split() == ["ID", "PRIMARY", "SECONDARY", "SIZE", "UPDATED", "PATH"]
    assert lines[1].split()[1:3] == ["-", "available"]
    assert lines[1].split()[-1] == "data/m.csv"


@pytest.mark.req("CLI-15")
def test_list_marks_a_never_copied_location_and_deleted_files(
    api: list[dict], tmp_path: Path
) -> None:
    run("add", str(_local(tmp_path, "p.csv", b"p")), "data/p.csv")
    listed = run("list").output.splitlines()
    assert listed[1].split()[1:3] == ["available", "-"]

    run("remove", "--path", "data/p.csv", "--yes")
    deleted = run("list", "--deleted").output.splitlines()
    assert deleted[0].split()[-2:] == ["UPDATED", "PATH"]
    assert "(deleted)" in deleted[1]


@pytest.mark.req("CLI-15")
def test_add_and_register_print_each_copy_state(
    api: list[dict], ctx: AppContext, tmp_path: Path
) -> None:
    _moved_to_secondary(ctx, tmp_path, tmp_path / "store2")
    added = run("add", str(_local(tmp_path, "n.csv", b"n")), "data/n.csv")
    assert added.output.split("\t")[1] == "primary=available"

"""Credentials from Prefect blocks: parsing, resolution, cache, redaction (STO-18, STO-19)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from heliopuddle.config import ConfigError, StorageLocationConfig, load_project
from heliopuddle.storage import StorageLocation
from heliopuddle.storage import credentials as creds
from heliopuddle.storage.credentials import (
    StorageLocationIncomplete,
    location_problems,
    open_location,
    resolve,
)
from heliopuddle.storage.redact import redact_text, redact_url


class FakeResolver:
    """In-memory Secret and RemoteFileSystem blocks."""

    def __init__(self) -> None:
        self.secrets: dict[str, Any] = {}
        self.filesystems: dict[str, tuple[str, dict[str, Any]]] = {}
        self.calls: list[tuple[str, str]] = []
        self.unreachable = False

    def secret(self, name: str) -> Any:
        self.calls.append(("secret", name))
        if self.unreachable:
            raise ConnectionError("prefect is down")
        if name not in self.secrets:
            raise LookupError(name)
        return self.secrets[name]

    def remote_file_system(self, name: str) -> tuple[str, dict[str, Any]]:
        self.calls.append(("fs", name))
        if self.unreachable:
            raise ConnectionError("prefect is down")
        if name not in self.filesystems:
            raise LookupError(name)
        return self.filesystems[name]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Iterator[Clock]:
    c = Clock()
    creds.set_clock(c)
    yield c
    creds.set_clock(None)


@pytest.fixture
def blocks(clock: Clock) -> Iterator[FakeResolver]:
    resolver = FakeResolver()
    creds.set_block_resolver(resolver)
    yield resolver
    creds.set_block_resolver(None)


def _cfg(**fields: Any) -> StorageLocationConfig:
    return StorageLocationConfig.model_validate({"name": "remote", **fields})


def _load(tmp_path: Path, body: str) -> StorageLocationConfig:
    path = tmp_path / "p.yaml"
    path.write_text(f"project: p\nstorage_locations:\n{body}")
    return load_project(path).storage_locations[0]


# --- config parsing -------------------------------------------------------------


@pytest.mark.req("STO-18")
@pytest.mark.parametrize(
    "value",
    [
        "{{ prefect.blocks.secret.key }}",
        "{{prefect.blocks.secret.key}}",
        "{{   prefect.blocks.secret.my-key-2   }}",
        "prefix-{{ prefect.blocks.secret.a }}-{{ prefect.blocks.secret.b }}",
    ],
)
def test_placeholder_syntax_is_accepted(tmp_path: Path, value: str) -> None:
    cfg = _load(
        tmp_path,
        f"  - name: a\n    url: memory://x\n    storage_options:\n      key: '{value}'\n",
    )
    assert cfg.storage_options == {"key": value}
    assert cfg.uses_blocks


@pytest.mark.req("STO-18")
@pytest.mark.parametrize(
    "value",
    [
        "{{ prefect.blocks.secret.Bad_Name }}",
        "{{ prefect.blocks.secret. }}",
        "{{ prefect.variables.x }}",
        "{{ prefect.blocks.secret.key }",
        "{{ unclosed",
    ],
)
def test_bad_placeholder_syntax_names_file_and_field(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError) as error:
        _load(
            tmp_path,
            f"  - name: a\n    url: memory://x\n    storage_options:\n"
            f"      nested:\n        key: '{value}'\n",
        )
    message = str(error.value)
    assert str(tmp_path / "p.yaml") in message
    assert "storage_locations.0.storage_options" in message
    assert "storage_options.nested.key" in message


@pytest.mark.req("STO-18")
def test_bad_placeholder_in_url_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"storage_locations\.0\.url"):
        _load(tmp_path, "  - name: a\n    url: 's3://{{ prefect.blocks.secret.X }}/p'\n")


@pytest.mark.req("STO-18")
def test_whole_storage_options_may_be_one_placeholder(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        "  - name: a\n    url: memory://x\n"
        "    storage_options: '{{ prefect.blocks.secret.opts }}'\n",
    )
    assert cfg.storage_options == "{{ prefect.blocks.secret.opts }}"
    with pytest.raises(ConfigError, match="mapping"):
        _load(tmp_path, "  - name: a\n    url: memory://x\n    storage_options: 'plain'\n")


@pytest.mark.req("STO-18")
def test_env_vars_still_expand_at_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HP_TEST_ACCOUNT", "acct")
    cfg = _load(
        tmp_path,
        "  - name: a\n    url: memory://x\n    storage_options:\n"
        "      account: ${HP_TEST_ACCOUNT}\n      key: '{{ prefect.blocks.secret.k }}'\n",
    )
    assert cfg.storage_options == {"account": "acct", "key": "{{ prefect.blocks.secret.k }}"}
    monkeypatch.delenv("HP_TEST_ACCOUNT")
    with pytest.raises(ConfigError, match="HP_TEST_ACCOUNT"):
        _load(
            tmp_path,
            "  - name: a\n    url: memory://x\n    storage_options:\n"
            "      account: ${HP_TEST_ACCOUNT}\n",
        )


@pytest.mark.req("STO-19")
def test_block_and_url_together_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        _load(tmp_path, "  - name: a\n    url: memory://x\n    block: my-fs\n")
    assert "storage location 'a': set exactly one of url and block" in str(error.value)
    with pytest.raises(ConfigError, match="exactly one of url and block"):
        _load(tmp_path, "  - name: a\n")


@pytest.mark.req("STO-19")
def test_block_location_display_url(tmp_path: Path) -> None:
    cfg = _load(tmp_path, "  - name: a\n    block: my-fs\n")
    assert cfg.url is None
    assert cfg.display_url == "prefect-block:my-fs"
    assert cfg.uses_blocks
    assert cfg.origin == f"{tmp_path / 'p.yaml'}: storage_locations.0"
    plain = _cfg(url="memory://x")
    assert plain.display_url == "memory://x"
    assert not plain.uses_blocks


# --- resolution -----------------------------------------------------------------


@pytest.mark.req("STO-18")
def test_plain_location_needs_no_resolver() -> None:
    creds.set_block_resolver(None)
    cfg = _cfg(url="memory://plain", storage_options={"a": 1})
    assert resolve(cfg) == ("memory://plain", {"a": 1})
    assert location_problems(cfg) == []
    assert isinstance(open_location(cfg), StorageLocation)


@pytest.mark.req("STO-18")
def test_placeholders_expand_in_nested_options_and_url(blocks: FakeResolver) -> None:
    blocks.secrets |= {"bucket": "my-bucket", "key": "k3y-value", "token": {"a": 1}}
    cfg = _cfg(
        url="memory://{{ prefect.blocks.secret.bucket }}/data",
        storage_options={
            "key": "{{ prefect.blocks.secret.key }}",
            "nested": {"list": ["x", "pre-{{ prefect.blocks.secret.key }}"]},
            "embedded_json": "t={{prefect.blocks.secret.token}}",
            "number": 3,
        },
    )
    url, options = resolve(cfg)
    assert url == "memory://my-bucket/data"
    assert options == {
        "key": "k3y-value",
        "nested": {"list": ["x", "pre-k3y-value"]},
        "embedded_json": 't={"a": 1}',
        "number": 3,
    }


@pytest.mark.req("STO-18")
def test_whole_value_placeholder_takes_json(blocks: FakeResolver) -> None:
    blocks.secrets |= {
        "json-str": '{"account_name": "acct", "anon": false}',
        "json-obj": {"account_name": "obj"},
        "plain": "not json",
        "numeric": "12345",
    }
    cfg = _cfg(
        url="memory://x",
        storage_options={
            "a": "{{ prefect.blocks.secret.json-str }}",
            "b": "{{ prefect.blocks.secret.json-obj }}",
            "c": "{{ prefect.blocks.secret.plain }}",
            "d": " {{ prefect.blocks.secret.numeric }} ",
        },
    )
    _, options = resolve(cfg)
    assert options["a"] == {"account_name": "acct", "anon": False}
    assert options["b"] == {"account_name": "obj"}
    assert options["c"] == "not json"
    assert options["d"] == "12345"  # a password that parses as a number stays a string


@pytest.mark.req("STO-18")
def test_whole_storage_options_from_one_secret(blocks: FakeResolver) -> None:
    blocks.secrets["azure-archive"] = '{"account_name": "acct", "account_key": "abcd1234"}'
    cfg = _cfg(url="memory://x", storage_options="{{ prefect.blocks.secret.azure-archive }}")
    assert resolve(cfg)[1] == {"account_name": "acct", "account_key": "abcd1234"}
    blocks.secrets["azure-archive"] = "just a string"
    creds.clear_cache()
    with pytest.raises(StorageLocationIncomplete, match="must hold a JSON object"):
        resolve(cfg)


@pytest.mark.req("STO-19")
def test_block_settings_merge_under_inline_options(blocks: FakeResolver) -> None:
    blocks.secrets["sas"] = "sas-token-value"
    blocks.filesystems["archive-fs"] = (
        "memory://archive",
        {"account_name": "acct", "client": {"timeout": 5, "retries": 2}, "anon": True},
    )
    cfg = _cfg(
        block="archive-fs",
        storage_options={
            "anon": False,
            "client": {"timeout": 30},
            "sas_token": "{{ prefect.blocks.secret.sas }}",
        },
    )
    url, options = resolve(cfg)
    assert url == "memory://archive"
    assert options == {
        "account_name": "acct",
        "client": {"timeout": 30, "retries": 2},
        "anon": False,
        "sas_token": "sas-token-value",
    }


@pytest.mark.req("STO-19")
def test_placeholders_in_block_settings_expand(blocks: FakeResolver) -> None:
    blocks.secrets["key"] = "from-secret"
    blocks.filesystems["fs"] = ("memory://fs", {"key": "{{ prefect.blocks.secret.key }}"})
    assert resolve(_cfg(block="fs")) == ("memory://fs", {"key": "from-secret"})


@pytest.mark.req("STO-18")
def test_missing_secret_names_file_field_and_block(tmp_path: Path, blocks: FakeResolver) -> None:
    cfg = _load(
        tmp_path,
        "  - name: a\n    url: memory://x\n    storage_options:\n"
        "      creds:\n        key: '{{ prefect.blocks.secret.azure-key }}'\n",
    )
    with pytest.raises(StorageLocationIncomplete) as error:
        resolve(cfg)
    (problem,) = error.value.problems
    assert problem == (
        f"{tmp_path / 'p.yaml'}: storage_locations.0.storage_options.creds.key: "
        "Prefect Secret block 'azure-key' not found"
    )
    assert str(error.value) == problem
    assert location_problems(cfg) == [problem]
    with pytest.raises(StorageLocationIncomplete):
        open_location(cfg)


@pytest.mark.req("STO-19")
def test_missing_filesystem_block_is_incomplete(blocks: FakeResolver) -> None:
    problems = location_problems(_cfg(block="nope"))
    assert problems == [
        "storage location 'remote'.block: Prefect RemoteFileSystem block 'nope' not found"
    ]


@pytest.mark.req("STO-18")
def test_unreachable_prefect_and_no_resolver_are_problems(blocks: FakeResolver) -> None:
    cfg = _cfg(url="memory://x", storage_options={"k": "{{ prefect.blocks.secret.k }}"})
    blocks.unreachable = True
    (problem,) = location_problems(cfg)
    assert "cannot load Prefect Secret block 'k': ConnectionError: prefect is down" in problem
    creds.set_block_resolver(None)
    (problem,) = location_problems(cfg)
    assert "Prefect is not connected in this process" in problem


@pytest.mark.req("STO-18")
def test_all_problems_are_reported(blocks: FakeResolver) -> None:
    cfg = _cfg(
        url="memory://{{ prefect.blocks.secret.a }}",
        storage_options={"x": "{{ prefect.blocks.secret.b }}"},
    )
    problems = location_problems(cfg)
    assert len(problems) == 2
    assert "'a' not found" in problems[0]
    assert "'b' not found" in problems[1]


@pytest.mark.req("STO-18")
def test_cache_expires_after_ttl(blocks: FakeResolver, clock: Clock) -> None:
    cfg = _cfg(url="memory://x", storage_options={"k": "{{ prefect.blocks.secret.k }}"})
    assert location_problems(cfg)  # missing: cached too
    blocks.secrets["k"] = "now-it-exists"
    clock.now += creds.CACHE_TTL - 1
    assert location_problems(cfg)  # still cached
    assert blocks.calls == [("secret", "k")]
    clock.now += 2
    assert location_problems(cfg) == []
    assert resolve(cfg)[1] == {"k": "now-it-exists"}
    assert blocks.calls == [("secret", "k"), ("secret", "k")]
    blocks.secrets["k"] = "rotated-value"
    assert resolve(cfg)[1] == {"k": "now-it-exists"}
    clock.now += creds.CACHE_TTL
    assert resolve(cfg)[1] == {"k": "rotated-value"}


@pytest.mark.req("STO-18")
def test_registered_checks_add_problems(blocks: FakeResolver) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def check(url: str, options: dict[str, Any]) -> list[str]:
        seen.append((url, options))
        return ["rclone config block 'r' not found"] if url.startswith("memory://bad") else []

    def broken(url: str, options: dict[str, Any]) -> list[str]:
        raise RuntimeError("check failed")

    creds.register_problem_check(check)
    creds.register_problem_check(check)  # once only
    try:
        assert location_problems(_cfg(url="memory://good")) == []
        assert location_problems(_cfg(url="memory://bad")) == ["rclone config block 'r' not found"]
        with pytest.raises(StorageLocationIncomplete, match="rclone config"):
            open_location(_cfg(url="memory://bad"))
        assert seen[0] == ("memory://good", {})
        creds.register_problem_check(broken)
        assert location_problems(_cfg(url="memory://good")) == ["RuntimeError: check failed"]
    finally:
        creds.unregister_problem_check(check)
        creds.unregister_problem_check(broken)
    assert location_problems(_cfg(url="memory://bad")) == []


@pytest.mark.req("STO-18")
def test_open_location_builds_the_requested_class(blocks: FakeResolver) -> None:
    class Counting(StorageLocation):
        pass

    blocks.secrets["root"] = "creds-root"
    store = open_location(
        _cfg(url="memory://{{ prefect.blocks.secret.root }}", create_root=True), Counting
    )
    assert isinstance(store, Counting)
    assert store.url == "memory://creds-root"


# --- redaction ------------------------------------------------------------------


@pytest.mark.req("STO-18")
@pytest.mark.req("STO-16")
def test_resolved_secrets_are_redacted(blocks: FakeResolver) -> None:
    blocks.secrets |= {
        "pw": "Unit-Test-Secret-Value-71",
        "opts": {"account_key": "Unit-Test-Nested-Secret-72"},
    }
    cfg = _cfg(
        url="sftp://bob:{{ prefect.blocks.secret.pw }}@host/x",
        storage_options={"o": "{{ prefect.blocks.secret.opts }}"},
    )
    resolve(cfg)
    message = "failed: key Unit-Test-Nested-Secret-72 and Unit-Test-Secret-Value-71"
    assert redact_text(message) == "failed: key *** and ***"
    assert redact_url("memory://Unit-Test-Secret-Value-71/x") == "memory://***/x"
    assert "Unit-Test-Secret-Value-71" not in redact_url("https://h/Unit-Test-Secret-Value-71?x=1")


@pytest.mark.req("STO-18")
def test_check_errors_do_not_leak_secrets(blocks: FakeResolver) -> None:
    blocks.secrets["pw"] = "Unit-Test-Secret-Value-73"

    def leaky(url: str, options: dict[str, Any]) -> list[str]:
        raise ValueError(f"bad option {options['pw']}")

    creds.register_problem_check(leaky)
    try:
        problems = location_problems(
            _cfg(url="memory://x", storage_options={"pw": "{{ prefect.blocks.secret.pw }}"})
        )
    finally:
        creds.unregister_problem_check(leaky)
    assert problems == ["ValueError: bad option ***"]


# --- startup --------------------------------------------------------------------


@pytest.mark.req("STO-18")
def test_startup_skips_an_incomplete_location(
    tmp_path: Path, blocks: FakeResolver, caplog: pytest.LogCaptureFixture
) -> None:
    from types import SimpleNamespace

    from heliopuddle.server.start import ensure_storage_roots

    good = tmp_path / "good"
    path = tmp_path / "p.yaml"
    path.write_text(
        "project: p\nstorage_locations:\n"
        f"  - name: plain\n    url: file://{good}\n"
        "  - name: vault\n    url: file://{{ prefect.blocks.secret.vault-root }}\n"
        "  - name: fs\n    block: missing-fs\n"
    )
    project = load_project(path)
    ctx = SimpleNamespace(projects={"p": project})
    ensure_storage_roots(ctx)  # type: ignore[arg-type]
    assert good.is_dir()
    assert not blocks.calls  # block users wait for Prefect
    with caplog.at_level(logging.WARNING, logger="heliopuddle.server.start"):
        ensure_storage_roots(ctx, blocks=True)  # type: ignore[arg-type]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "'vault' of project 'p' is incomplete" in warnings[0]
    assert "storage_locations.1.url: Prefect Secret block 'vault-root' not found" in warnings[0]
    assert "Prefect RemoteFileSystem block 'missing-fs' not found" in warnings[1]

    blocks.secrets["vault-root"] = str(tmp_path / "vault")
    creds.clear_cache()
    ensure_storage_roots(ctx, blocks=True)  # type: ignore[arg-type]
    assert (tmp_path / "vault").is_dir()

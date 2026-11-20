"""The signing key (AUTH-06) and its use in tokens (AUTH-05)."""

from __future__ import annotations

import stat
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from heliopuddle.server.auth.secret import SecretKeyError, load_secret
from heliopuddle.settings import Settings


def make(tmp_path: Path, **kwargs: object) -> Settings:
    return Settings(config_dir=tmp_path / "c", data_dir=tmp_path / "d", **kwargs)  # type: ignore[arg-type]


@pytest.mark.req("AUTH-06")
def test_env_key_wins_and_writes_no_file(tmp_path: Path) -> None:
    key = "from-the-environment-" + "k" * 20
    s = make(tmp_path, auth_secret_key=key)
    assert load_secret(s) == key.encode()
    assert not (tmp_path / "d" / "auth_secret").exists()


@pytest.mark.req("AUTH-06")
def test_key_is_generated_with_mode_0600_and_reused(tmp_path: Path) -> None:
    s = make(tmp_path)
    key = load_secret(s)
    path = tmp_path / "d" / "auth_secret"
    assert len(key) == 32
    assert path.read_bytes() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_secret(s) == key
    assert load_secret(make(tmp_path)) == key  # another process
    assert [p.name for p in path.parent.iterdir()] == ["auth_secret"]  # no temp files left


@pytest.mark.req("AUTH-06")
def test_concurrent_creators_agree(tmp_path: Path) -> None:
    s = make(tmp_path)
    keys: list[bytes] = []
    barrier = threading.Barrier(8)

    def run() -> None:
        barrier.wait()
        keys.append(load_secret(s))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(keys) == 8
    assert len(set(keys)) == 1
    assert (tmp_path / "d" / "auth_secret").read_bytes() == keys[0]


@pytest.mark.req("AUTH-06")
def test_unreadable_key_file_fails_fast_naming_the_variable(tmp_path: Path) -> None:
    s = make(tmp_path)
    (tmp_path / "d").mkdir()
    # A directory where the file should be: neither readable nor creatable.
    (tmp_path / "d" / "auth_secret").mkdir()
    with pytest.raises(SecretKeyError, match="HELIOPUDDLE_AUTH_SECRET_KEY"):
        load_secret(s)


@pytest.mark.req("AUTH-06")
def test_uncreatable_data_dir_fails_fast_naming_the_variable(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    s = Settings(config_dir=tmp_path / "c", data_dir=blocker / "d")
    with pytest.raises(SecretKeyError, match="HELIOPUDDLE_AUTH_SECRET_KEY"):
        load_secret(s)


@pytest.mark.req("AUTH-06")
@pytest.mark.parametrize("value", ["", " ", "short", "x" * 31])
def test_an_empty_or_short_key_is_rejected_by_the_settings_naming_the_variable(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(ValidationError, match="auth_secret_key") as error:
        make(tmp_path, auth_secret_key=value)
    assert "at least 32" in str(error.value)
    assert "openssl rand" in str(error.value)


@pytest.mark.req("AUTH-06")
def test_the_empty_environment_variable_stops_startup_instead_of_using_a_known_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from heliopuddle.server.start import StartupError, load_settings

    monkeypatch.setenv("HELIOPUDDLE_AUTH_SECRET_KEY", "")  # e.g. `${KEY:-}` in a compose file
    with pytest.raises(StartupError, match="HELIOPUDDLE_AUTH_SECRET_KEY"):
        load_settings()


@pytest.mark.req("AUTH-06")
@pytest.mark.parametrize("value", ["", "short"])
def test_the_loader_refuses_a_weak_key_even_if_settings_validation_was_bypassed(
    tmp_path: Path, value: str
) -> None:
    from pydantic import SecretStr

    weak = make(tmp_path).model_copy(update={"auth_secret_key": SecretStr(value)})
    with pytest.raises(SecretKeyError, match="HELIOPUDDLE_AUTH_SECRET_KEY"):
        load_secret(weak)
    assert not (tmp_path / "d" / "auth_secret").exists()  # no fallback to a generated key


@pytest.mark.req("AUTH-06")
def test_a_key_of_exactly_the_minimum_length_is_accepted(tmp_path: Path) -> None:
    assert load_secret(make(tmp_path, auth_secret_key="k" * 32)) == b"k" * 32

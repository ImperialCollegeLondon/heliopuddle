"""Project config loading: ${VAR} expansion and fail-fast errors (DEP-05, API-03)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from heliopuddle.config import ConfigError, load_project, load_projects
from heliopuddle.config.loader import default_config_path, has_project_files
from heliopuddle.config.models import DEFAULT_VERSION_RULES, AgeCriteria, AgeRange

GOOD = """\
project: demo
storage_locations:
  - name: primary
    url: file://${HP_TEST_ROOT:-/data}
    storage_options:
      account_key: ${HP_TEST_KEY}
      nested:
        - plain
        - "${HP_TEST_KEY}-x"
      retries: 3
"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text)
    return path


@pytest.mark.req("API-03", "DEP-05")
def test_load_project_expands_env_in_url_and_storage_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HP_TEST_KEY", "s3cret")
    monkeypatch.delenv("HP_TEST_ROOT", raising=False)
    project = load_project(_write(tmp_path, "demo.yaml", GOOD))

    loc = project.storage_location()
    assert loc.url == "file:///data"
    assert loc.storage_options == {
        "account_key": "s3cret",
        "nested": ["plain", "s3cret-x"],
        "retries": 3,
    }


@pytest.mark.req("API-03")
def test_env_value_overrides_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HP_TEST_KEY", "k")
    monkeypatch.setenv("HP_TEST_ROOT", "/srv/store")
    project = load_project(_write(tmp_path, "demo.yaml", GOOD))
    assert project.storage_location("primary").url == "file:///srv/store"


@pytest.mark.req("DEP-05")
def test_unset_env_without_default_names_file_and_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HP_TEST_KEY", raising=False)
    path = _write(tmp_path, "demo.yaml", GOOD)
    with pytest.raises(ConfigError) as exc:
        load_project(path)
    msg = str(exc.value)
    assert str(path) in msg
    assert "storage_locations.0.storage_options.account_key" in msg
    assert "HP_TEST_KEY" in msg


@pytest.mark.req("DEP-05")
def test_unset_env_in_url_names_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HP_TEST_MISSING", raising=False)
    text = "project: p\nstorage_locations:\n  - name: a\n    url: file://${HP_TEST_MISSING}\n"
    with pytest.raises(ConfigError, match=r"storage_locations\.0\.url: .*HP_TEST_MISSING"):
        load_project(_write(tmp_path, "p.yaml", text))


@pytest.mark.req("DEP-05")
@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("project: [unclosed\n", "invalid YAML"),
        ("- just\n- a list\n", "expected a mapping"),
        ("project: p\n", "storage_locations"),
        ("project: p\nstorage_locations: []\n", "storage_locations"),
        ("project: p\nstorage_locations:\n  - name: a\n", "storage_locations.0.url"),
        ("project: 'bad name'\nstorage_locations:\n  - {name: a, url: 'file:///x'}\n", "project"),
        (
            "project: p\nstorage_locations:\n  - {name: a, url: 'file:///x'}\n"
            "  - {name: a, url: 'file:///y'}\n",
            "duplicate storage location names: a",
        ),
        (
            "project: p\nstorage_locations:\n  - {name: a, url: 'file:///x', colour: red}\n",
            "storage_locations.0.colour",
        ),
        ("project: p\nstorage_locations:\n  - {name: a, url: 'file:///x'}\nfoo: 1\n", "foo"),
    ],
)
def test_invalid_project_file_names_file_and_field(
    tmp_path: Path, text: str, fragment: str
) -> None:
    path = _write(tmp_path, "p.yaml", text)
    with pytest.raises(ConfigError) as exc:
        load_project(path)
    assert str(path) in str(exc.value)
    assert fragment in str(exc.value)


@pytest.mark.req("DEP-05")
def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_project(tmp_path / "absent.yaml")


@pytest.mark.req("DEP-05")
def test_load_projects_keys_by_name_and_reads_yml(tmp_path: Path) -> None:
    _write(
        tmp_path, "a.yaml", "project: alpha\nstorage_locations:\n  - {name: s, url: 'file:///a'}\n"
    )
    _write(
        tmp_path, "b.yml", "project: beta\nstorage_locations:\n  - {name: s, url: 'file:///b'}\n"
    )
    _write(tmp_path, "notes.txt", "ignored")
    projects = load_projects(tmp_path)
    assert list(projects) == ["alpha", "beta"]
    assert projects["beta"].storage_location().url == "file:///b"


@pytest.mark.req("DEP-05")
def test_load_projects_duplicate_names(tmp_path: Path) -> None:
    text = "project: same\nstorage_locations:\n  - {name: s, url: 'file:///a'}\n"
    _write(tmp_path, "a.yaml", text)
    _write(tmp_path, "b.yaml", text)
    with pytest.raises(
        ConfigError, match=r"b\.yaml: project: duplicate project name 'same'.*a\.yaml"
    ):
        load_projects(tmp_path)


@pytest.mark.req("DEP-05")
def test_load_projects_empty_or_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no project files"):
        load_projects(tmp_path)
    with pytest.raises(ConfigError, match="not a directory"):
        load_projects(tmp_path / "nope")


@pytest.mark.req("DEP-10")
@pytest.mark.parametrize("config", ["nope", "empty"])
def test_load_projects_without_project_files_uses_the_default(tmp_path: Path, config: str) -> None:
    config_dir = tmp_path / config
    if config == "empty":
        config_dir.mkdir()
        (config_dir / "README").write_text("no projects here")
    data = tmp_path / "data"

    projects = load_projects(config_dir, default_data_dir=data)

    cfg = projects["default"]
    assert list(projects) == ["default"]
    assert [s.name for s in cfg.storage_locations] == ["primary", "archive"]
    location = cfg.storage_location()
    assert location.url == (data / "primary-storage").resolve().as_uri()
    assert location.create_root == "auto"
    assert not has_project_files(config_dir)
    assert not (data / "primary-storage").exists()  # created by startup, not by loading
    # the setup is written to a config file, which is what was loaded
    written = data / "default_config.yaml"
    assert default_config_path(data) == written
    assert written.is_file()
    assert load_project(written) == cfg
    assert not config_dir.exists() or not (config_dir / "default_config.yaml").exists()


@pytest.mark.req("DEP-10")
def test_default_config_has_two_locations_version_rules_and_an_archive_rule(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"

    cfg = load_projects(tmp_path / "nope", default_data_dir=data)["default"]

    root = data.resolve()
    assert {s.name: s.url for s in cfg.storage_locations} == {
        "primary": (root / "primary-storage").as_uri(),
        "archive": (root / "archive-storage").as_uri(),
    }
    assert cfg.versioning == list(DEFAULT_VERSION_RULES)
    [rule] = cfg.rules
    assert (rule.name, rule.action, rule.source, rule.destination) == (
        "auto-archive-old-versions-hourly",
        "move",
        "primary",
        "archive",
    )
    assert rule.match.is_latest is False
    assert rule.match.age == AgeCriteria(modified=AgeRange(older_than=timedelta(hours=1)))
    assert rule.schedule == "0 * * * *" and rule.options.batch.max_files == 10
    text = (data / "default_config.yaml").read_text()
    assert text.startswith("# Generated by heliopuddle")
    assert "@PRIMARY@" not in text and "@ARCHIVE@" not in text


@pytest.mark.req("DEP-10")
def test_an_existing_default_config_file_is_loaded_as_is(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    edited = "project: mine\nstorage_locations:\n  - {name: disk, url: 'file:///elsewhere'}\n"
    (data / "default_config.yaml").write_text(edited)

    projects = load_projects(tmp_path / "nope", default_data_dir=data)

    assert list(projects) == ["mine"]
    assert (data / "default_config.yaml").read_text() == edited  # never overwritten


@pytest.mark.req("DEP-10")
def test_default_config_is_not_written_when_project_files_exist(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", "project: a\nstorage_locations:\n  - {name: p, url: 'file:///x'}\n")
    data = tmp_path / "data"

    load_projects(tmp_path, default_data_dir=data)

    assert not data.exists()


@pytest.mark.req("DEP-10")
def test_unwritable_default_config_is_a_config_error(tmp_path: Path) -> None:
    blocker = tmp_path / "data"
    blocker.write_text("a file, not a directory")

    with pytest.raises(ConfigError, match=r"default_config\.yaml"):
        load_projects(tmp_path / "nope", default_data_dir=blocker)


@pytest.mark.req("DEP-10")
def test_any_project_file_replaces_the_default(tmp_path: Path) -> None:
    _write(tmp_path, "a.yaml", "project: a\nstorage_locations:\n  - {name: p, url: 'file:///x'}\n")

    assert list(load_projects(tmp_path, default_data_dir=tmp_path / "data")) == ["a"]
    assert has_project_files(tmp_path)


@pytest.mark.req("DEP-10")
def test_invalid_project_files_are_still_an_error_with_a_default(tmp_path: Path) -> None:
    _write(tmp_path, "bad.yml", "project: [\n")

    with pytest.raises(ConfigError, match=r"bad\.yml"):
        load_projects(tmp_path, default_data_dir=tmp_path / "data")


@pytest.mark.req("DEP-05")
def test_example_config_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HELIOPUDDLE_DEMO_DATA", raising=False)
    examples = Path(__file__).parents[2] / "examples" / "projects"
    projects = load_projects(examples)
    assert projects["demo"].storage_location().url == "file:///data"


_RULES_BASE = "project: demo\nstorage_locations:\n  - {name: s, url: 'file:///x'}\nversioning:\n"


_GROUPS = "'_v(?P<major>\\d+)\\.(?P<minor>\\d+)\\.(?P<ext>[^./]+)$'"


@pytest.mark.req("DEP-06")
@pytest.mark.parametrize(
    ("rules", "field", "message"),
    [
        (
            "  - {paths: ['**'], extraction_pattern: '(?P<version>'}\n",
            "versioning.0.extraction_pattern",
            "invalid regex",
        ),
        ("  - {paths: ['**'], pattern: 'v\\d+'}\n", "versioning.0.pattern", "Extra inputs"),
        (
            "  - {paths: ['**'], extraction_pattern: 'x', latest_by: minor}\n",
            "versioning.0.latest_by",
            "Extra inputs",
        ),
        (
            "  - {paths: ['**'], extraction_pattern: 'x', x: 1}\n",
            "versioning.0.x",
            "Extra inputs",
        ),
        (
            "  - {paths: ['**'], extraction_pattern: 'x', version_type: semver}\n",
            "versioning.0.version_type",
            "'number' or 'text'",
        ),
        (
            "  - {paths: ['a/**'], extraction_pattern: 'x'}\n"
            f"  - {{paths: ['**'], extraction_pattern: {_GROUPS}, version_text: '{{major}}'}}\n",
            "versioning.1.version_text",
            "only allowed with version_type: text",
        ),
        (
            f"  - {{paths: ['**'], extraction_pattern: {_GROUPS}, version_type: text,\n"
            "     version_text: '{patch}'}\n",
            "versioning.0.version_text",
            "not a named group",
        ),
        (
            "  - {paths: ['**'], extraction_pattern: '(?P<date>\\d{8})', version_type: text,\n"
            "     version_text: '{date}'}\n",
            "versioning.0.version_text",
            "'date' group",
        ),
        (
            f"  - {{paths: ['**'], extraction_pattern: {_GROUPS}, version_type: text,\n"
            "     version_text: '{0}'}\n",
            "versioning.0.version_text",
            "must be the name of a group",
        ),
        (
            f"  - {{paths: ['**'], extraction_pattern: {_GROUPS}, version_type: text,\n"
            "     version_text: '{major:03d}'}\n",
            "versioning.0.version_text",
            "does not format",
        ),
        (
            f"  - {{paths: ['**'], extraction_pattern: {_GROUPS}, ignore_in_series: [patch]}}\n",
            "versioning.0.ignore_in_series",
            "not a named group",
        ),
        (
            "  - {paths: ['**'], extraction_pattern: '(?P<date>\\d{8})',\n"
            "     ignore_in_series: [date]}\n",
            "versioning.0.ignore_in_series",
            "'date' group",
        ),
        ("  - {paths: [], extraction_pattern: 'x'}\n", "versioning.0.paths", "at least 1"),
        ("  - {paths: [' '], extraction_pattern: 'x'}\n", "versioning.0.paths", "empty"),
        ("  - {extraction_pattern: 'x'}\n", "versioning.0.paths", "required"),
        ("  - {paths: ['**']}\n", "versioning.0.extraction_pattern", "required"),
    ],
)
def test_invalid_version_rule_names_file_index_and_field(
    tmp_path: Path, rules: str, field: str, message: str
) -> None:
    path = tmp_path / "demo.yaml"
    path.write_text(_RULES_BASE + rules)
    with pytest.raises(ConfigError) as err:
        load_projects(tmp_path)
    text = str(err.value)
    assert f"{path}: {field}: " in text, text
    assert message in text, text


@pytest.mark.req("DEP-06")
def test_valid_version_rules_load(tmp_path: Path) -> None:
    (tmp_path / "demo.yaml").write_text(
        _RULES_BASE
        + "  - paths: ['cal/**']\n"
        + "    version_type: text\n"
        + f"    extraction_pattern: {_GROUPS}\n"
        + "    version_text: '{major:0>3}.{minor:0>4}'\n"
        + "    ignore_in_series: [ext]\n"
        + "  - {paths: ['k/*', 'j/**'], extraction_pattern: '_(?P<version>\\d{2})\\.'}\n"
    )
    cfg = load_projects(tmp_path)["demo"]
    assert [r.version_type for r in cfg.versioning] == ["text", "number"]
    assert cfg.versioning[0].version_text == "{major:0>3}.{minor:0>4}"
    assert cfg.versioning[0].ignore_in_series == ["ext"]
    assert cfg.versioning[1].paths == ["k/*", "j/**"]


@pytest.mark.req("DEP-06", "API-02")
@pytest.mark.parametrize(("tail", "count"), [("", 1), ("versioning: []\n", 0)])
def test_versioning_unset_gets_the_default_and_empty_turns_it_off(
    tmp_path: Path, tail: str, count: int
) -> None:
    (tmp_path / "demo.yaml").write_text(_RULES_BASE.removesuffix("versioning:\n") + tail)
    cfg = load_projects(tmp_path)["demo"]
    assert cfg.versioning == list(DEFAULT_VERSION_RULES)[:count]


@pytest.mark.req("STO-01")
@pytest.mark.req("STO-08")
@pytest.mark.parametrize("key", ["create_root"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "auto"), ("auto", "auto"), ("true", True), ("false", False)],
)
def test_auto_true_false_settings(
    tmp_path: Path, key: str, value: str | None, expected: object
) -> None:
    line = f"    {key}: {value}\n" if value is not None else ""
    text = f"project: demo\nstorage_locations:\n  - name: p\n    url: file:///data\n{line}"
    location = load_project(_write(tmp_path, "demo.yaml", text)).storage_location()
    assert getattr(location, key) == expected


@pytest.mark.req("STO-01")
@pytest.mark.req("STO-08")
@pytest.mark.parametrize("key", ["create_root"])
def test_auto_true_false_settings_reject_other_values(tmp_path: Path, key: str) -> None:
    text = (
        "project: demo\nstorage_locations:\n  - name: p\n    url: file:///data\n"
        f"    {key}: sometimes\n"
    )
    with pytest.raises(ConfigError, match=key):
        load_project(_write(tmp_path, "demo.yaml", text))


@pytest.mark.req("STO-01")
def test_removed_temp_writes_setting_fails_fast(tmp_path: Path) -> None:
    # F10: temp_writes is gone; an old config naming it is rejected at load.
    text = (
        "project: demo\nstorage_locations:\n  - name: p\n    url: file:///data\n"
        "    temp_writes: true\n"
    )
    with pytest.raises(ConfigError, match="temp_writes"):
        load_project(_write(tmp_path, "demo.yaml", text))

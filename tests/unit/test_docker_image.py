"""The Docker image's defaults, examples and publishing rules (DEP-15, DEP-16)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from heliopuddle.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docker"
WORKFLOW = ROOT / ".github" / "workflows" / "docker.yml"
EXAMPLES = [DOCKER / "compose.yaml", DOCKER / "compose.external.yaml"]
ENV_EXAMPLE = DOCKER / "heliopuddle.env.example"


def _workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML reads the bare key `on` as True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def _tag_rules() -> list[str]:
    steps = _workflow()["jobs"]["image"]["steps"]
    meta = next(s for s in steps if s.get("id") == "meta")
    return [line.strip() for line in meta["with"]["tags"].splitlines() if line.strip()]


@pytest.mark.req("DEP-16")
def test_publish_triggers_are_version_tags_main_pull_requests_and_manual_runs() -> None:
    on = _workflow()["on"]
    assert on["push"] == {"tags": ["v*"], "branches": ["main"]}
    assert on["pull_request"] == {"branches": ["main"]}
    assert "workflow_dispatch" in on


@pytest.mark.req("DEP-16")
def test_manual_builds_only_ever_get_dev_tags() -> None:
    rules = _tag_rules()
    manual = [r for r in rules if "github.event_name == 'workflow_dispatch'" in r]
    assert len(manual) == 2
    assert all("value=dev-" in r for r in manual)
    pr = [r for r in rules if "github.event_name == 'pull_request'" in r]
    others = [r for r in rules if r not in manual and r not in pr]
    # latest, main and version tags are only for pushes, never for a manual run
    # (even one started on a tag) or a pull request.
    assert others and all("github.event_name == 'push'" in r for r in others)
    assert all("dev-" not in r and "pr-" not in r for r in others)


@pytest.mark.req("DEP-16")
def test_pull_requests_get_only_pr_tags_from_the_head_commit() -> None:
    pr = [r for r in _tag_rules() if "github.event_name == 'pull_request'" in r]
    assert len(pr) == 2
    assert all("value=pr-${{ github.event.pull_request.number }}" in r for r in pr)
    # The head commit, not the merge commit GitHub checks out for a PR.
    assert any("steps.pr.outputs.sha" in r for r in pr)
    sha = next(s for s in _steps() if s.get("id") == "pr")
    assert "github.event.pull_request.head.sha" in str(sha["env"])


@pytest.mark.req("DEP-16")
def test_pull_request_images_go_to_ghcr_only_and_never_from_forks() -> None:
    env = _workflow()["env"]
    # A fork's GITHUB_TOKEN cannot write packages: build and smoke test only.
    assert env["PUSH"] == (
        "${{ github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository }}"
    )
    assert env["DOCKERHUB"] == (
        "${{ vars.DOCKERHUB_REPOSITORY != '' && github.event_name != 'pull_request' }}"
    )
    logins = [s for s in _steps() if s.get("uses", "").startswith("docker/login-action")]
    assert all("env.PUSH == 'true'" in s["if"] for s in logins)
    push = next(s for s in _steps() if s.get("name") == "build and push")
    assert push["if"] == "env.PUSH == 'true'"
    assert push["with"]["platforms"] == (
        "${{ github.event_name == 'pull_request' && 'linux/amd64' || 'linux/amd64,linux/arm64' }}"
    )
    assert _workflow()["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name != 'push' }}"
    )


@pytest.mark.req("DEP-16")
def test_release_tags_are_semver_and_latest_skips_prereleases() -> None:
    rules = " ".join(_tag_rules())
    for pattern in ("{{version}}", "{{major}}.{{minor}}", "{{major}}"):
        assert f"type=semver,pattern={pattern}," in rules
    latest = next(r for r in _tag_rules() if "value=latest" in r)
    assert "github.ref_type == 'tag'" in latest and "!contains(github.ref_name, '-')" in latest
    main = next(r for r in _tag_rules() if "value=main" in r)
    assert "github.ref == 'refs/heads/main'" in main


@pytest.mark.req("DEP-16")
def test_images_are_smoke_tested_and_checked_for_test_tooling_before_push() -> None:
    steps = _workflow()["jobs"]["image"]["steps"]
    names = [s.get("name", s.get("uses", "")) for s in steps]
    push = names.index("build and push")
    checks = names.index("no test tooling or test files in the image")
    smoke = names.index("smoke test (zero-config start, SQLite, embedded Prefect)")
    assert checks < push and smoke < push
    assert "docker/image_checks.py" in steps[checks]["run"]
    version = next(s for s in steps if s.get("name") == "check the tag matches the package version")
    assert "pyproject.toml" in version["run"]


def _setting_names(text: str) -> set[str]:
    return set(re.findall(r"\bHELIOPUDDLE_[A-Z0-9_]+\b", text)) - {"HELIOPUDDLE_IMAGE"}


@pytest.mark.req("DEP-15")
@pytest.mark.parametrize("path", [*EXAMPLES, ENV_EXAMPLE], ids=lambda p: p.name)
def test_example_settings_are_real_settings(path: Path) -> None:
    known = {f"HELIOPUDDLE_{name.upper()}" for name in Settings.model_fields}
    names = _setting_names(path.read_text())
    assert names <= known, sorted(names - known)


@pytest.mark.req("DEP-15")
def test_external_example_uses_env_vars_for_postgres_prefect_and_oidc() -> None:
    compose = yaml.safe_load((DOCKER / "compose.external.yaml").read_text())
    env = compose["services"]["heliopuddle"]["environment"]
    assert env["HELIOPUDDLE_DATABASE_URL"].startswith("postgresql+psycopg://")
    assert env["PREFECT_API_URL"] == "http://prefect:4200/api"
    oidc = ENV_EXAMPLE.read_text()
    for name in ("AUTH_MODE=oidc", "OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
        assert f"HELIOPUDDLE_{name}" in oidc


@pytest.mark.req("DEP-15")
def test_image_defaults_to_sqlite_in_the_state_volume_and_files_in_data() -> None:
    dockerfile = (DOCKER / "Dockerfile").read_text()
    assert "HELIOPUDDLE_DATA_DIR=/var/lib/heliopuddle" in dockerfile
    assert "HELIOPUDDLE_DATABASE_URL" not in dockerfile  # SQLite under the data dir
    assert 'VOLUME ["/data", "/config", "/var/lib/heliopuddle"]' in dockerfile
    standalone = yaml.safe_load((DOCKER / "compose.yaml").read_text())
    volumes = standalone["services"]["heliopuddle"]["volumes"]
    assert "hp-state:/var/lib/heliopuddle" in volumes and "hp-data:/data" in volumes
    assert "environment" not in standalone["services"]["heliopuddle"]


def _steps() -> list[dict[str, Any]]:
    return _workflow()["jobs"]["image"]["steps"]


def _meta_images() -> list[str]:
    meta = next(s for s in _steps() if s.get("id") == "meta")
    return [line.strip() for line in meta["with"]["images"].splitlines() if line.strip()]


@pytest.mark.req("DEP-16")
def test_images_always_go_to_the_github_container_registry() -> None:
    assert _workflow()["permissions"] == {"contents": "read", "packages": "write"}
    images = _meta_images()
    ghcr = [i for i in images if "steps.names.outputs.ghcr" in i]
    assert len(ghcr) == 1 and "enable=" not in ghcr[0]  # always on
    logins = [s for s in _steps() if s.get("uses", "").startswith("docker/login-action")]
    github = next(s for s in logins if s["with"].get("registry") == "ghcr.io")
    assert github["if"] == "env.PUSH == 'true'"  # skipped only for fork pull requests
    assert github["with"]["username"] == "${{ github.actor }}"
    assert github["with"]["password"] == "${{ secrets.GITHUB_TOKEN }}"
    # ghcr.io needs a lowercase name; the owner may have capitals.
    names = next(s for s in _steps() if s.get("id") == "names")
    assert "ghcr=ghcr.io/${GHCR_REPOSITORY,,}" in names["run"]
    assert "vars.GHCR_REPOSITORY || github.repository" in _workflow()["env"]["GHCR_REPOSITORY"]


@pytest.mark.req("DEP-16")
def test_docker_hub_is_an_opt_in_extra_target() -> None:
    hub_enabled = "env.DOCKERHUB == 'true'"
    assert "vars.DOCKERHUB_REPOSITORY != ''" in _workflow()["env"]["DOCKERHUB"]
    images = _meta_images()
    hub = [i for i in images if "steps.names.outputs.dockerhub" in i]
    assert len(hub) == 1 and f"enable=${{{{ {hub_enabled} }}}}" in hub[0]
    names = next(s for s in _steps() if s.get("id") == "names")
    assert "dockerhub=docker.io/${DOCKERHUB_REPOSITORY,,}" in names["run"]
    logins = [s for s in _steps() if s.get("uses", "").startswith("docker/login-action")]
    docker_hub = next(s for s in logins if "registry" not in s["with"])
    assert hub_enabled in docker_hub["if"]
    check = next(s for s in _steps() if s.get("name") == "check Docker Hub credentials")
    assert check["if"] == hub_enabled


@pytest.mark.req("DEP-16")
def test_published_images_link_back_to_the_repository() -> None:
    meta = next(s for s in _steps() if s.get("id") == "meta")
    assert "org.opencontainers.image.source=" in meta["with"]["labels"]


CANONICAL_IMAGE = "ghcr.io/imperialcollegelondon/heliopuddle"


@pytest.mark.req("DEP-16")
@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_compose_examples_default_to_the_published_image(path: Path) -> None:
    services = yaml.safe_load(path.read_text())["services"]
    assert services["heliopuddle"]["image"] == f"${{HELIOPUDDLE_IMAGE:-{CANONICAL_IMAGE}:latest}}"


@pytest.mark.req("DEP-16")
@pytest.mark.parametrize("doc", ["README.md", "docs/design/deployment.md"])
def test_docs_run_the_published_image(doc: str) -> None:
    text = (ROOT / doc).read_text()
    runs = re.findall(r"docker run [^`]*?(?=\n```)", text, flags=re.S)
    hp_runs = [r for r in runs if "/var/lib/heliopuddle" in r]
    assert hp_runs and all(CANONICAL_IMAGE in r for r in hp_runs)


@pytest.mark.req("DEP-20")
def test_prefect_port_is_published_on_loopback_only() -> None:
    for compose in EXAMPLES:
        ports = [
            p
            for service in yaml.safe_load(compose.read_text())["services"].values()
            for p in service.get("ports", [])
            if str(p).endswith(":4200")
        ]
        assert ports, compose
        assert all(str(p).startswith("127.0.0.1:") for p in ports), (compose, ports)
    for doc in (ROOT / "docs" / "design" / "deployment.md", ROOT / "README.md"):
        text = doc.read_text()
        assert not re.search(r"-p\s+4200:4200", text), doc
    deployment = (ROOT / "docs" / "design" / "deployment.md").read_text()
    assert "no authentication" in deployment
    assert "-p 127.0.0.1:4200:4200" in deployment

"""pytest fixtures for testing code that calls heliopuddle (CLI-14).

Registered as a ``pytest11`` entry point, so the fixtures are available in any
project that installs heliopuddle, with no conftest changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from heliopuddle.testing.fake import FakeHelioPuddleClient

# The plugin is loaded at pytest start-up, so it imports nothing heavier than
# pytest until a fixture is used: projects that never use it pay nothing.


@pytest.fixture
def heliopuddle_store(tmp_path: Path) -> Path:
    """An empty directory acting as the fake's ``primary`` storage location."""
    path = tmp_path / "heliopuddle-store"
    path.mkdir()
    return path


@pytest.fixture
def heliopuddle_fake(heliopuddle_store: Path) -> Iterator[FakeHelioPuddleClient]:
    """A :class:`FakeHelioPuddleClient` for project ``default`` over ``heliopuddle_store``."""
    from heliopuddle.testing.fake import FakeHelioPuddleClient

    with FakeHelioPuddleClient(project="default", store=heliopuddle_store, user="test") as fake:
        yield fake

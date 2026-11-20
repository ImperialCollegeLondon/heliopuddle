"""Rule notifications survive the process that made the change ending (SVC-24)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = """
import sys
import time
from types import SimpleNamespace

from heliopuddle.server.services import rule_control, rule_notify


def slow_notify(ctx, project, ids, sources):
    time.sleep(0.5)
    with open(sys.argv[1], "w") as out:
        out.write(f"{project}:{list(ids)}")


rule_notify._notify = slow_notify
ctx = SimpleNamespace(projects={"demo": SimpleNamespace(rules=[object()])})
rule_control.notify_file_changes(ctx, "demo", [7])
# The process ends at once, as a flow-run process does after publishing a copy.
"""


@pytest.mark.req("SVC-24")
def test_a_process_that_exits_right_after_a_change_still_sends_the_notification(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "notified"
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(marker)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert marker.read_text() == "demo:[7]"

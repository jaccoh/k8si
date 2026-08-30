"""DOM-level behavioural tests for dashboard.html via the jsdom harness.

The source-regex tests in test_dashboard_html.py prove functions exist; this
module actually executes the dashboard's script in jsdom and drives it (click
sort headers, assert row order, chip state, queued wiring). Skips when node
+ jsdom are unavailable — CI runners are python-only; run `npm install` at the
repo root to enable it locally.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
HARNESS = REPO / "scripts" / "dashboard_dom_test.mjs"


def _harness_available() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    try:
        subprocess.run(
            [node, "-e", "require.resolve('jsdom')"],
            cwd=REPO,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _harness_available(), reason="node + jsdom not installed (run: npm install)"
)


def test_dashboard_dom_behaviour() -> None:
    proc = subprocess.run(
        ["node", str(HARNESS)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert lines, (
        f"harness produced no JSON result line\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )

    results = json.loads(lines[-1])
    assert proc.returncode == 0, f"DOM harness failures: {results['fail']}"
    assert len(results["pass"]) >= 9, f"expected >= 9 checks to run, got {results}"

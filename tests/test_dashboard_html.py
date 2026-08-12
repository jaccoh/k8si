"""Smoke checks for k8si/ui/dashboard.html's inline <style> block."""

import re
from pathlib import Path

DASHBOARD_HTML = Path(__file__).parent.parent / "k8si" / "ui" / "dashboard.html"


def _extract_style_block() -> str:
    html = DASHBOARD_HTML.read_text()
    match = re.search(r"<style>(.*?)</style>", html, re.DOTALL)
    assert match, "dashboard.html has no <style> block"
    return match.group(1)


def test_style_block_braces_are_balanced():
    css = _extract_style_block()
    opens = css.count("{")
    closes = css.count("}")
    assert opens == closes, (
        f"unbalanced braces in dashboard.html <style> block: {opens} '{{' vs {closes} '}}' "
        "— a missing '}' silently nests the following rules inside the previous selector"
    )


def test_open_backup_logs_falls_back_to_last_run_ref():
    html = DASHBOARD_HTML.read_text()
    match = re.search(r"function openBackupLogs\(ns, nm\) \{(.*?)\n  \}", html, re.DOTALL)
    assert match, "openBackupLogs function not found"
    body = match.group(1)
    assert "lastRunRef" in body, (
        "openBackupLogs must fall back to backup.lastRunRef when recentRuns is empty, same "
        "as buildStatusBadge does -- otherwise the Logs button silently degrades to the legacy "
        "per-backup log stream on any backup whose recentRuns array isn't populated, while the "
        "status-icon click (which uses lastRunRef directly) keeps working. Inconsistent per-row."
    )

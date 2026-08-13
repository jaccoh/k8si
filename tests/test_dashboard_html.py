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


def _extract_function(name: str) -> str:
    html = DASHBOARD_HTML.read_text()
    match = re.search(r"function " + re.escape(name) + r"\([^)]*\) \{(.*?)\n  \}", html, re.DOTALL)
    assert match, f"{name} function not found"
    return match.group(1)


def test_open_backup_logs_falls_back_to_last_run_ref():
    body = _extract_function("openBackupLogs")

    last_run_ref_pos = body.find("backup.lastRunRef")
    open_logs_pos = body.find("openLogs(ns, nm)")
    assert last_run_ref_pos != -1, (
        "openBackupLogs must fall back to backup.lastRunRef when recentRuns is empty, same "
        "as buildStatusBadge does -- otherwise the Logs button silently degrades to the legacy "
        "per-backup log stream on any backup whose recentRuns array isn't populated, while the "
        "status-icon click (which uses lastRunRef directly) keeps working. Inconsistent per-row."
    )
    assert open_logs_pos != -1, (
        "openBackupLogs must still fall back to the legacy openLogs() when neither recentRuns "
        "nor lastRunRef are available"
    )
    assert last_run_ref_pos < open_logs_pos, (
        "the lastRunRef fallback must be checked BEFORE giving up and calling openLogs() -- "
        "otherwise every backup without a populated recentRuns array falls straight to the "
        "legacy per-backup stream even when lastRunRef is available"
    )
    assert "openRunLogs(ns, backup.lastRunRef" in body, (
        "the lastRunRef fallback must route through openRunLogs (the run-specific tab), not "
        "the legacy openLogs(), so it lands in the same tab id scheme as the status-icon click "
        "and correctly shows as a run tab rather than a legacy per-backup tab"
    )


def test_escape_html_escapes_single_quotes():
    """escapeHtml()'s output is spliced directly into single-quoted JS string
    literals inside onclick="..." attributes, e.g.:

        onclick="openRunLogsWithPicker('${ns}', '${escapeHtml(nm)}', ...)"

    in buildStatusBadge, buildSparkline, and buildRow. The interpolated values
    (status.lastRunRef, recentRuns[].name, namespace/name) come from the
    K8siBackup CRD, which places no restriction on these strings (see
    deploy/crd.yaml) -- so they are effectively attacker/user controlled.

    If escapeHtml does not neutralize `'`, a name such as
    `x');alert(document.cookie);//` breaks out of the JS string literal and
    injects arbitrary script that runs on click. Escaping `<`, `>`, `&`, `"`
    alone (HTML-attribute escaping) does not protect a JS-string-literal
    context -- `'` must also be escaped.
    """
    body = _extract_function("escapeHtml")
    assert re.search(r"\.replace\(/'/g,\s*['\"]&#39;['\"]\)", body), (
        "escapeHtml must escape single quotes (e.g. to &#39;) -- without this, "
        'a CRD-sourced run/backup name containing "\'" breaks out of the '
        "single-quoted JS string literal inside onclick=\"...('...')\" handlers "
        "built by buildStatusBadge/buildSparkline/buildRow, enabling script "
        "injection (e.g. name = x');alert(document.cookie);// )"
    )

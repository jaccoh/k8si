"""Smoke checks for k8si/ui/dashboard.html's inline <style> and <script> blocks."""

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
        "— a missing '}}' silently nests the following rules inside the previous selector"
    )


def _extract_function(name: str) -> str:
    html = DASHBOARD_HTML.read_text()
    match = re.search(r"function " + re.escape(name) + r"\([^)]*\) \{(.*?)\n  \}", html, re.DOTALL)
    assert match, f"{name} function not found"
    return match.group(1)


def test_open_backup_logs_prefers_live_run():
    """The Logs button must show the LIVE run while one is active — not a stale
    completed one from recentRuns (mirror image of the lastRunRef fix 437bbfa:
    recentRuns was checked where activeRunState/lastRunRef should win)."""
    body = _extract_function("openBackupLogs")

    live_pos = body.find("activeRunState[ns + '/' + nm]")
    running_pos = body.find("backup.lastBackupResult === 'running'")
    recent_pos = body.find("historicalRuns[0]")
    assert live_pos != -1 and running_pos != -1 and recent_pos != -1, (
        "openBackupLogs must consider activeRunState, a running lastRunRef, and "
        "historicalRuns[0] as candidates"
    )
    assert live_pos < recent_pos, (
        "the activeRunState live run must be checked BEFORE falling back to "
        "historicalRuns[0] — otherwise a stale completed run is shown while a "
        "new one is live"
    )
    assert running_pos < recent_pos, (
        "a backup whose lastBackupResult is running must win over historical completed runs too"
    )
    assert "openRunLogs(ns, backup.lastRunRef" in body, (
        "the lastRunRef fallback must route through openRunLogs (the run-specific tab)"
    )


def test_legacy_open_logs_path_removed():
    """The legacy per-backup log stream (openLogs → /api/backups/{ns}/{name}/logs)
    is dead code in the UI: the UI ServiceAccount has no RBAC to read pod logs,
    so the stream sits silent for 10 minutes and then paints a GREEN 'success'
    dot for a stream that never delivered anything. It must not exist."""
    html = DASHBOARD_HTML.read_text()
    assert "function openLogs(" not in html, (
        "the legacy openLogs() path must be deleted from the dashboard — it "
        "cannot work (no pods/log RBAC) and falsifies success"
    )


def test_close_tab_clears_active_run_state():
    """Closing a log tab mid-run must clear the activeRunState entry tied to
    that tab's run. The entry's only other cleanup is the 'done' SSE message
    delivered over the very stream the close just destroyed — without this,
    the row badge is frozen on the stale phase forever (buildRow prefers
    activeRunState over polled data) and scheduleFastPoll re-arms a 3s poll
    indefinitely (goals-doc #14)."""
    body = _extract_function("closeTab")
    assert "releaseActiveRunForTab(tab)" in body, (
        "closeTab must release the activeRunState entry for the closed tab's run"
    )

    helper = _extract_function("releaseActiveRunForTab")
    assert "activeRunState" in helper and "delete" in helper, (
        "releaseActiveRunForTab must delete the matching activeRunState entry"
    )
    assert "st.runName === tab.targetName" in helper, (
        "only release the entry when it refers to THIS tab's run — a newer run "
        "of the same backup (opened in another tab) must keep its entry"
    )


def test_stream_error_releases_active_run_state():
    """es.onerror must release activeRunState too: a network blip mid-run kills
    the stream without a 'done' message, stranding the entry with the same
    frozen-row + eternal-fast-poll symptoms as closing the tab."""
    html = DASHBOARD_HTML.read_text()
    m = re.search(r"es\.onerror = function\(\) \{(.*?)\n    \};", html, re.DOTALL)
    assert m and "releaseActiveRunForTab" in m.group(1), (
        "the SSE error handler in openRunLogs must release the activeRunState entry"
    )


def test_open_run_logs_caps_concurrent_tabs():
    """EventSource connections are capped (~6 per origin on HTTP/1.1); unbounded
    log tabs starve the dashboard's own /api/backups polling (goals-doc #14)."""
    html = DASHBOARD_HTML.read_text()
    assert re.search(r"MAX_LOG_TABS = \d+", html), "a MAX_LOG_TABS cap must be defined"
    body = _extract_function("openRunLogs")
    assert "MAX_LOG_TABS" in body and "closeTab(logTabs[0].id)" in body, (
        "openRunLogs must evict the oldest tab when the cap is reached"
    )


def test_tabs_carry_backup_name():
    """Tabs must store the backup name directly instead of re-deriving it from
    allBackups/recentRuns scans — the scan returns '' when the run isn't found
    (e.g. an older run picked from the picker), which propagates an empty
    backup name and breaks the picker (goals-doc #14)."""
    html = DASHBOARD_HTML.read_text()
    assert re.search(r"function openRunLogs\(namespace, runName, backupName,", html), (
        "openRunLogs must take an explicit backupName argument"
    )
    body = _extract_function("openRunLogs")
    assert "backupName: backupName || runName" in body, "the tab object must store backupName"

    picker = _extract_function("updateTabHeaderAndDot")
    assert "picker.dataset.backupName = tab.backupName" in picker, (
        "the run picker must take the backup name from the tab, not a recentRuns scan"
    )
    assert "allBackups.find" not in picker, (
        "updateTabHeaderAndDot must not re-derive the backup via allBackups scan"
    )

    activate = _extract_function("activateTab")
    assert "activeTab.backupName" in activate, (
        "the Logs-button highlight must match on the tab's backupName (it used "
        "the run name, which never matches for run tabs)"
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

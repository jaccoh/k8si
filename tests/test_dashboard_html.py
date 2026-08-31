"""Smoke checks for k8si/ui/dashboard.html's inline <style> and <script> blocks."""

import re
from pathlib import Path

UI_DIR = Path(__file__).parent.parent / "k8si" / "ui"
DASHBOARD_HTML = UI_DIR / "dashboard.html"
DASHBOARD_CSS = UI_DIR / "static" / "app.css"
DASHBOARD_JS = UI_DIR / "static" / "app.js"


def _bundle() -> str:
    """Shell + JS + CSS concatenated — most checks only care that something
    in the dashboard carries the marker, wherever the split put it."""
    return DASHBOARD_HTML.read_text() + DASHBOARD_JS.read_text() + DASHBOARD_CSS.read_text()


def _extract_style_block() -> str:
    css = DASHBOARD_CSS.read_text()
    assert css.strip(), "static/app.css is empty"
    return css


def test_style_block_braces_are_balanced():
    css = _extract_style_block()
    opens = css.count("{")
    closes = css.count("}")
    assert opens == closes, (
        f"unbalanced braces in dashboard.html <style> block: {opens} '{{' vs {closes} '}}' "
        "— a missing '}}' silently nests the following rules inside the previous selector"
    )


def _extract_function(name: str) -> str:
    html = _bundle()
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
    html = _bundle()
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
    html = _bundle()
    m = re.search(r"es\.onerror = function\(\) \{(.*?)\n    \};", html, re.DOTALL)
    assert m and "releaseActiveRunForTab" in m.group(1), (
        "the SSE error handler in openRunLogs must release the activeRunState entry"
    )


def test_open_run_logs_caps_concurrent_tabs():
    """EventSource connections are capped (~6 per origin on HTTP/1.1); unbounded
    log tabs starve the dashboard's own /api/backups polling (goals-doc #14)."""
    html = _bundle()
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
    html = _bundle()
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


def test_mutating_calls_use_token_aware_fetch():
    """Goal #2: trigger/pause fetches must go through apiFetch (attaches the
    optional X-K8si-Token from localStorage and retries once after a 401
    prompt) — a bare fetch would fail permanently once K8SI_UI_TOKEN is set."""
    html = _bundle()
    assert "function apiFetch" in html, "apiFetch wrapper missing"
    assert "'X-K8si-Token'" in html, "token header not attached"
    assert "fetch('/api/backups/' +" not in html, (
        "mutating calls must not use bare fetch — they would bypass the token"
    )
    assert html.count("apiFetch('/api/backups/") == 2, (
        "both mutating endpoints (trigger + paused) must use apiFetch"
    )


# ── 0.10.0: table layout, sorting, queued status ─────────────────────────────


def test_msg_cell_is_block_level():
    """.msg-cell ellipsis is inert on an inline <span>: max-width/overflow/
    text-overflow only apply to block-level boxes, so one long unbreakable
    error message stretched the whole table sideways (user report 2026-08-30
    screenshot). The full text stays available via the title tooltip."""
    css = _extract_style_block()
    m = re.search(r"\.msg-cell\s*\{([^}]*)\}", css)
    assert m, ".msg-cell rule missing from dashboard styles"
    assert re.search(r"display:\s*block", m.group(1)), (
        ".msg-cell must be display:block — on an inline span the ellipsis rules "
        "do nothing and long messages blow out the table width"
    )
    assert re.search(r"overflow:\s*hidden", m.group(1))
    assert re.search(r"text-overflow:\s*ellipsis", m.group(1))


def test_tables_use_fixed_layout_and_shared_colgroup():
    """Every namespace renders its own <table>; with auto layout their column
    widths drift apart (visible in the 2026-08-30 screenshot) and content can
    still stretch a column. table-layout:fixed + one shared <colgroup> keeps
    all sections aligned and caps every column."""
    css = _extract_style_block()
    m = re.search(r"\.backup-table\s*\{([^}]*)\}", css)
    assert m, ".backup-table rule missing"
    assert re.search(r"table-layout:\s*fixed", m.group(1)), (
        ".backup-table must use table-layout:fixed for deterministic aligned columns"
    )
    body = _extract_function("buildTable")
    assert "<colgroup>" in body, "buildTable must emit a colgroup"
    widths = re.search(r"widths\s*=\s*\[([^\]]+)\]", body)
    assert widths and len(widths.group(1).split(",")) == 8, (
        "the colgroup must size all 8 columns (one width per column)"
    )


def test_column_headers_are_sortable():
    """Clicking a column header sorts ascending, clicking again descending,
    with a visible indicator (user request 2026-08-30). Headers carry
    data-key; toggleSort flips direction; setSortIndicators maintains
    aria-sort; render applies the sort before grouping."""
    body = _extract_function("buildTable")
    for key in ("name", "status", "lastBackupTime", "nextBackupTime", "successRate"):
        assert f"sortableTh('{key}'" in body, f"header for {key} must be sortable"
    th_src = _extract_function("sortableTh")
    assert "data-key=" in th_src, "sortable headers must carry a data-key attribute"
    assert "toggleSort(" in th_src, "headers must toggle sorting on click"

    html = _bundle()
    assert "function toggleSort(" in html, "toggleSort missing"
    assert "function clearSort(" in html, "clearSort missing"
    assert "function setSortIndicators(" in html, "setSortIndicators missing"
    assert "aria-sort" in html, "sort state must be exposed via aria-sort"

    render_body = _extract_function("render")
    assert re.search(r"sortKey.*\.sort\(|\.sort\(cmpBackups", render_body), (
        "render must apply the active sort before grouping into namespace sections"
    )


def test_status_rank_covers_all_five_phases():
    """Sorting by status needs a defined order for every phase the CRD can
    report — including the new 'queued' (needs-attention order: running <
    queued < failed < pending < success)."""
    html = _bundle()
    m = re.search(r"STATUS_RANK\s*=\s*\{([^}]*)\}", html)
    assert m, "STATUS_RANK map missing from dashboard script"
    for phase in ("running", "queued", "failed", "pending", "success"):
        assert phase in m.group(1), f"STATUS_RANK must rank the '{phase}' phase"


def test_queued_status_wired_into_views_and_polling():
    """The operator reports 'queued' between trigger and Job start (0.10.0).
    The dashboard must expose it: a sidebar view, a stat card, and fast
    polling while anything is queued (the slow 30s cadence would otherwise
    lag the queued→running flip)."""
    html = _bundle()
    assert 'data-filter="queued"' in html, "sidebar needs a Queued view"
    assert 'id="badge-queued"' in html, "sidebar Queued view needs a count badge"
    assert 'id="stat-queued"' in html, "summary strip needs a Queued stat card"
    poll = _extract_function("scheduleFastPoll")
    assert "'queued'" in poll, "scheduleFastPoll must fast-poll while a backup is queued"

    labels = _extract_function("render")
    assert "Queued backups" in labels, "the queued filter needs a section title"


# ── 0.10.0: action buttons layout ────────────────────────────────────────────


def test_actions_are_one_aligned_group_not_a_flex_td():
    """The actions cell used to be a <td style="display:flex"> — a td that is
    no longer a table-cell falls out of the fixed column grid and its buttons
    STRETCH to the row height (visibly crooked on taller rows; user report
    2026-08-30 "stone-age and crooked"). All row actions (Backup now, Pause,
    Logs) must live in ONE <div class="actions-cell"> inside a normal td, and
    no td may carry display:flex."""
    html = _bundle()
    body = _extract_function("buildRow")
    assert 'actions-cell"' in body, "buildRow must wrap the action buttons in div.actions-cell"
    assert body.index("triggerBtn") < body.index("actions-cell")
    assert "logsBtn" in body.split("actions-cell")[1], (
        "the Logs button belongs in the same actions group — the old lone "
        "Logs column clipped off-screen behind an empty message column"
    )
    assert not re.search(r"<td[^>]*display:\s*flex", html), (
        "no td may use display:flex — it breaks the fixed table grid and "
        "stretches the buttons to the row height"
    )
    assert "<th>Logs</th>" not in _extract_function("buildTable"), (
        "the separate Logs header column is gone; Logs lives in the actions group"
    )


def test_action_buttons_share_consistent_sizing():
    """Backup/Pause/Logs buttons must be identical compact squares so they
    line up across every row regardless of row height."""
    css = _extract_style_block()
    for cls in ("btn-trigger", "btn-logs"):
        m = re.search(rf"\.{cls}\s*\{{([^}}]*)\}}", css)
        assert m, f".{cls} rule missing"
        assert re.search(r"display:\s*inline-flex", m.group(1)), f".{cls} must be inline-flex"
        assert re.search(r"align-items:\s*center", m.group(1)), f".{cls} must center its icon"
        assert re.search(r"height:\s*26px", m.group(1)), f".{cls} must have a fixed height"
        assert re.search(r"width:\s*26px", m.group(1)), f".{cls} must be a compact square"


def test_action_buttons_are_icon_only_with_tooltips():
    """User feedback 2026-08-30: text buttons ('Backup now' / 'Pause' / 'Logs')
    looked heavy and dated. Buttons are icon-only now: every button carries
    its meaning in aria-label + title (tooltip), the icon is inline SVG that
    inherits currentColor, and runtime state changes go through button
    classes + title — never textContent, which would wipe the SVG."""
    body = _extract_function("buildRow")
    for label in ("Backup now", "Pause", "Resume", "Logs"):
        assert f'aria-label="{label}"' in body, f"icon button needs aria-label '{label}'"
    html = _bundle()
    assert "<svg" in html and 'fill="currentColor"' in html, (
        "buttons must render inline SVG icons that inherit currentColor for hover states"
    )

    trigger = _extract_function("triggerBackup")
    assert "btn.textContent" not in trigger, (
        "triggerBackup must not set textContent on icon buttons — it wipes the "
        "SVG; state goes through classes + title"
    )
    assert "setBtnState" in trigger, "state changes must go through setBtnState"
    assert "btn.title" in _extract_function("setBtnState"), (
        "setBtnState must update the tooltip (btn.title) — icon-only buttons "
        "carry their state text there"
    )


def test_run_state_is_data_driven_with_toast_errors():
    """SSE callbacks must not mutate button DOM refs — every render() detaches
    them (fast-poll re-renders every 3s during a run). The live phase in
    activeRunState drives the freshly built button (buildRow), so callbacks
    only update state + render(). Error feedback goes through a toast — the
    stale-ref path could never show it anyway."""
    html = _bundle()
    assert 'id="toast"' in html, "a toast element is needed for error feedback"
    assert "function showToast" in html, "showToast missing"
    assert re.search(r"\.toast\s*\{", _extract_style_block()), "toast needs styling"

    trigger = _extract_function("triggerBackup")
    assert "showToast" in trigger, "trigger failures must surface via showToast"
    # The only allowed button touch is the immediate click feedback before the
    # first render(); the SSE/done/error paths must be pure state.
    after_render = trigger.split("render();", 1)[1] if "render();" in trigger else trigger
    assert "btn." not in after_render, (
        "after the optimistic render() the clicked button is detached — only "
        "activeRunState + render() may drive the UI from there"
    )

    paused = _extract_function("setPaused")
    assert "showToast" in paused, "pause failures must surface via showToast"


def test_dashboard_shell_is_split_from_assets():
    """The dashboard was a ~2000-line single HTML file. Since the split the
    shell must stay lean: no inline <style>, no inline scripts — only the
    /static asset references. Regressing to inline blocks re-creates the
    monolith one paste at a time."""
    shell = DASHBOARD_HTML.read_text()
    assert "<style>" not in shell and "</style>" not in shell, "no inline CSS in the shell"
    assert '<script src="/static/app.js"></script>' in shell, "JS must load from /static"
    assert '<link rel="stylesheet" href="/static/app.css">' in shell, "CSS must load from /static"
    body_scripts = [ln for ln in shell.splitlines() if "<script" in ln and "src=" not in ln]
    assert not body_scripts, f"inline script blocks found: {body_scripts}"
    assert len(shell.splitlines()) < 200, "the shell must stay lean — assets live in static/"
    assert len(DASHBOARD_JS.read_text().splitlines()) > 500, (
        "app.js should carry the bulk of the logic"
    )

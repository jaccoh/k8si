  // State
  let allBackups = [];
  let activeFilter = 'all';
  let searchQuery = '';
  let lastFetchTime = null;
  let refreshCounterInterval = null;
  let fastPollTimer = null;
  // Client-side run state: key = "ns/name" → {runName, phase: 'queued'|'running'}
  let activeRunState = {};

  // Column sorting: sortKey = column key, sortDir = 1 asc / -1 desc.
  // Shared by every namespace section — one click sorts them all.
  let sortKey = null;
  let sortDir = 1;

  // EventSource connections are capped (~6 per origin on HTTP/1.1) and the
  // dashboard itself polls /api/backups — bound the number of live log tabs.
  let MAX_LOG_TABS = 4;

  // ── Utility: relative time ──────────────────────────────────────────────────

  function relativeTime(isoString) {
    if (!isoString) return null;
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHour = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHour / 24);

    if (diffSec < 60) return 'just now';
    if (diffMin < 60) return diffMin === 1 ? '1 minute ago' : diffMin + ' minutes ago';
    if (diffHour < 24) return diffHour === 1 ? '1 hour ago' : diffHour + ' hours ago';
    if (diffDay === 1) return 'yesterday';
    if (diffDay < 30) return diffDay + ' days ago';
    const diffMonth = Math.floor(diffDay / 30);
    return diffMonth === 1 ? '1 month ago' : diffMonth + ' months ago';
  }

  function relativeFuture(isoString) {
    if (!isoString) return null;
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = date - now;
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHour = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHour / 24);

    if (diffMs < 0) return 'overdue';
    if (diffSec < 60) return 'in moments';
    if (diffMin < 60) return 'in ' + diffMin + (diffMin === 1 ? ' minute' : ' minutes');
    if (diffHour < 24) return 'in ' + diffHour + (diffHour === 1 ? ' hour' : ' hours');
    return 'in ' + diffDay + (diffDay === 1 ? ' day' : ' days');
  }

  function formatDateTime(isoString) {
    if (!isoString) return null;
    const date = new Date(isoString);
    const day = String(date.getDate()).padStart(2, '0');
    const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const month = monthNames[date.getMonth()];
    const hours = String(date.getHours()).padStart(2, '0');
    const mins = String(date.getMinutes()).padStart(2, '0');
    return day + ' ' + month + ' ' + hours + ':' + mins;
  }

  function secondsSinceLastFetch() {
    if (!lastFetchTime) return null;
    return Math.floor((Date.now() - lastFetchTime) / 1000);
  }

  function refreshLabel(seconds) {
    if (seconds === null) return 'Loading…';
    if (seconds < 5) return 'Updated just now';
    if (seconds < 60) return 'Updated ' + seconds + 's ago';
    const mins = Math.floor(seconds / 60);
    return 'Updated ' + mins + (mins === 1 ? ' minute ago' : ' minutes ago');
  }

  // ── Sparkline builder ───────────────────────────────────────────────────────

  function resultToClass(result) {
    if (result === 'success') return 'ok';
    if (result === 'failed') return 'err';
    if (result === 'running') return 'run';
    return 'dim';
  }

  function buildSparkline(recentRuns, ns, nm) {
    const TARGET = 7;
    // Take up to 7, newest-first from API, so reverse to get oldest-first
    const entries = (recentRuns || []).slice(0, TARGET).reverse();
    const bars = [];

    // Pad with dim bars on the left if fewer than 7 entries
    const padCount = TARGET - entries.length;
    for (let i = 0; i < padCount; i++) {
      bars.push('<div class="history-bar dim"></div>');
    }
    for (const entry of entries) {
      const cls = resultToClass(entry.result);
      const title = escapeHtml(entry.result + (entry.time ? ' · ' + formatDateTime(entry.time) : ''));
      if (entry.name && ns && nm) {
        bars.push('<div class="history-bar ' + cls + ' clickable" title="' + title + '"'
          + ' onclick="openRunLogsWithPicker(\'' + escapeHtml(ns) + '\',\'' + escapeHtml(nm)
          + '\',\'' + escapeHtml(entry.name) + '\')" style="cursor:pointer"></div>');
      } else {
        bars.push('<div class="history-bar ' + cls + '" title="' + title + '"></div>');
      }
    }

    return '<div class="history">' + bars.join('') + '</div>';
  }

  // ── Column sorting ──────────────────────────────────────────────────────────

  var STATUS_RANK = {running: 0, queued: 1, failed: 2, pending: 3, success: 4};
  var SORT_LABELS = {
    name: 'Name',
    status: 'Status',
    lastBackupTime: 'Last backup',
    nextBackupTime: 'Next backup',
    successRate: 'Success rate'
  };
  // Natural first click per column: names A→Z, statuses by attention, times
  // and success rates newest/best first.
  var SORT_DEFAULT_DIR = {name: 1, status: 1, lastBackupTime: -1, nextBackupTime: -1, successRate: -1};

  function effectiveStatus(b) {
    var live = activeRunState[(b.namespace || '') + '/' + (b.name || '')];
    if (live) return live.phase;
    return b.lastBackupResult || 'pending';
  }

  function sortValue(b, key) {
    if (key === 'name') return (b.name || '').toLowerCase();
    if (key === 'status') {
      var rank = STATUS_RANK[effectiveStatus(b)];
      return rank === undefined ? 99 : rank;
    }
    if (key === 'lastBackupTime') {
      var lastTs = Date.parse(b.lastBackupTime || '');
      return isNaN(lastTs) ? null : lastTs;
    }
    if (key === 'nextBackupTime') {
      var nextTs = Date.parse(b.nextBackupTime || '');
      return isNaN(nextTs) ? null : nextTs;
    }
    if (key === 'successRate') return typeof b.successRate === 'number' ? b.successRate : null;
    return null;
  }

  function cmpBackups(key) {
    return function(a, b) {
      var va = sortValue(a, key), vb = sortValue(b, key);
      var aEmpty = va === null || va === undefined, bEmpty = vb === null || vb === undefined;
      if (aEmpty && bEmpty) return 0;
      if (aEmpty) return 1;  // missing values (never-backed-up, no rate) always last
      if (bEmpty) return -1;
      var r = typeof va === 'number' ? va - vb : String(va).localeCompare(String(vb));
      return r * sortDir;
    };
  }

  function toggleSort(key) {
    if (sortKey === key) {
      sortDir = -sortDir;
    } else {
      sortKey = key;
      sortDir = SORT_DEFAULT_DIR[key] || 1;
    }
    render();
  }

  function clearSort() {
    sortKey = null;
    sortDir = 1;
    render();
  }

  function setSortIndicators() {
    var ths = document.querySelectorAll('.backup-table th[data-key]');
    for (var i = 0; i < ths.length; i++) {
      var el = ths[i];
      var ind = el.querySelector('.sort-ind');
      if (!ind) continue;
      if (el.getAttribute('data-key') === sortKey) {
        el.setAttribute('aria-sort', sortDir > 0 ? 'ascending' : 'descending');
        ind.textContent = sortDir > 0 ? '▲' : '▼';
      } else {
        el.removeAttribute('aria-sort');
        ind.textContent = '';
      }
    }
  }

  function updateSortChip() {
    var chip = document.getElementById('sort-chip');
    if (!chip) return;
    if (!sortKey) {
      chip.style.display = 'none';
      chip.textContent = '';
      return;
    }
    chip.style.display = '';
    chip.textContent = 'Sorted: ' + (SORT_LABELS[sortKey] || sortKey) + (sortDir > 0 ? ' ↑' : ' ↓');
    chip.title = 'Click to clear sorting';
  }

  // ── HTML builders ───────────────────────────────────────────────────────────

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/`/g, '&#96;');
  }

  function buildHistoryStats(backup) {
    const rate = backup.successRate;
    const streak = backup.streak;
    if (rate === null && streak === 0) return '';
    const rateStr = rate !== null ? Math.round(rate * 100) + '%' : '';
    let streakStr = '';
    if (streak > 0) {
      streakStr = '<span class="streak-pos">+' + streak + '</span>';
    } else if (streak < 0) {
      streakStr = '<span class="streak-neg">' + streak + '</span>';
    }
    const parts = [];
    if (rateStr) parts.push(rateStr);
    if (streakStr) parts.push(streakStr);
    return '<div class="history-stats">' + parts.join('<span style="color:#30363d">·</span>') + '</div>';
  }

  function formatDuration(secs) {
    if (secs === null || secs === undefined) return null;
    if (secs < 60) return secs + 's';
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    return s > 0 ? m + 'm ' + s + 's' : m + 'm';
  }

  function buildStatusBadge(backup, statusClass, statusLabel, runRef) {
    var inner = '<span class="status-dot"></span>' + statusLabel;
    if (runRef && statusClass !== 'pending') {
      var ns = escapeHtml(backup.namespace);
      var nm = escapeHtml(backup.name);
      var rr = escapeHtml(runRef);
      var title = statusClass === 'running' ? 'Click to view live logs' : 'Click to view logs';
      return '<span class="status ' + statusClass + '" onclick="openRunLogsWithPicker(\''
        + ns + '\',\'' + nm + '\',\'' + rr + '\')" title="' + title + '" style="cursor:pointer">'
        + inner + '</span>';
    }
    return '<span class="status ' + statusClass + '">' + inner + '</span>';
  }

  // Icon-only action buttons: the icon carries no text — meaning lives in
  // aria-label + title (tooltip). fill=currentColor makes hover/state colors
  // apply to the glyph itself.
  const ICON_PLAY = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M5 3.2v9.6l7.6-4.8z"/></svg>';
  const ICON_PAUSE = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M4.6 3h2.4v10H4.6zM9 3h2.4v10H9z"/></svg>';
  const ICON_CONSOLE = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M2.5 2.5h11a.5.5 0 0 1 .5.5v10a.5.5 0 0 1-.5.5h-11A.5.5 0 0 1 2 13V3a.5.5 0 0 1 .5-.5zm2 2.9-.9 1 2.1 2.1-2.1 2.1.9 1 3.1-3.1-3.1-3.1zm4.5 4.6h3V9.1h-3v.9z"/></svg>';

  function buildRow(backup) {
    const runKey = backup.namespace + '/' + backup.name;
    const liveRun = activeRunState[runKey];
    const statusClass = liveRun ? escapeHtml(liveRun.phase) : escapeHtml(backup.lastBackupResult || 'pending');
    const statusLabel = statusClass;
    const runRef = liveRun ? liveRun.runName : backup.lastRunRef;

    const lastTime = backup.lastBackupTime ? formatDateTime(backup.lastBackupTime) : null;
    const lastAgo  = backup.lastBackupTime ? relativeTime(backup.lastBackupTime) : null;
    const nextTime = backup.nextBackupTime ? formatDateTime(backup.nextBackupTime) : null;
    const nextIn   = backup.nextBackupTime ? relativeFuture(backup.nextBackupTime) : null;
    const durStr   = formatDuration(backup.lastBackupDuration);

    const lastCell = lastTime
      ? '<div class="time-cell"><span class="time-main">' + escapeHtml(lastTime) + '</span><span class="time-ago">' + escapeHtml(lastAgo) + '</span>'
        + (durStr ? '<span class="time-duration">took ' + escapeHtml(durStr) + '</span>' : '')
        + '</div>'
      : '<span class="time-never">never</span>';

    const nextCell = nextTime
      ? '<div class="time-cell"><span class="time-main">' + escapeHtml(nextTime) + '</span><span class="time-ago">' + escapeHtml(nextIn) + '</span></div>'
      : '<span class="time-never">—</span>';

    const msgClass = (backup.lastBackupResult === 'failed') ? 'msg-cell error' : 'msg-cell';
    const msgText  = escapeHtml(backup.message || '');

    const ns = escapeHtml(backup.namespace);
    const nm = escapeHtml(backup.name);
    const pauseBtn = backup.paused
      ? '<button class="btn-trigger" aria-label="Resume" title="Resume" onclick="setPaused(\''
        + ns + '\',\'' + nm + '\',false,this)">' + ICON_PLAY + '</button>'
      : '<button class="btn-trigger" aria-label="Pause" title="Pause" onclick="setPaused(\''
        + ns + '\',\'' + nm + '\',true,this)">' + ICON_PAUSE + '</button>';
    // The clicked button gets detached by every render() (fast-poll re-renders
    // every 3s during a run), so the LIVE phase must drive the freshly built
    // button too — otherwise it renders enabled and stateless mid-run,
    // inviting a second click that the backend rejects with a 409.
    const livePhase = liveRun ? liveRun.phase : null;
    const btnState = livePhase === 'queued' || livePhase === 'running' ? livePhase : null;
    const btnTitle = btnState === 'queued' ? 'Queued…' : btnState === 'running' ? 'Running…' : 'Backup now';
    const triggerBtn = '<button class="btn-trigger' + (btnState ? ' ' + btnState : '') + '"'
      + ' aria-label="Backup now" title="' + btnTitle + '"'
      + ((backup.paused || btnState) ? ' disabled' : '')
      + ' onclick="triggerBackup(\'' + ns + '\',\'' + nm + '\',this)">' + ICON_PLAY + '</button>';
    const pausedBadge = backup.paused ? '<span class="badge-paused">paused</span>' : '';
    const restoreBadge = (backup.lastRestoreResult === 'failed')
      ? '<span class="badge-restore-failed" title="' + escapeHtml(backup.lastRestoreMessage || '') + '">restore failed</span>'
      : '';

    const win = backup.backupWindow || {};
    const windowLine = (win.start && win.end)
      ? '<span class="schedule-window">' + escapeHtml(win.start) + ' – ' + escapeHtml(win.end) + ' UTC</span>'
      : '';
    const logsBtn = '<button class="btn-logs" aria-label="Logs" title="Logs" data-ns="' + ns + '" data-nm="' + nm
      + '" onclick="openBackupLogs(\'' + ns + '\',\'' + nm + '\')">' + ICON_CONSOLE + '</button>';

    return '<tr data-result="' + statusClass + '" data-name="' + nm + '" data-ns="' + ns + '">'
      + '<td><div class="app-cell">'
      + '<span class="app-name">' + nm + pausedBadge + restoreBadge + '</span>'
      + '<span class="app-ns">' + ns + '</span>'
      + '</div></td>'
      + '<td>' + buildStatusBadge(backup, statusClass, statusLabel, runRef) + '</td>'
      + '<td>' + lastCell + '</td>'
      + '<td>' + nextCell + '</td>'
      + '<td><span class="schedule-tag">' + escapeHtml(backup.schedule || '—') + '</span>' + windowLine + '</td>'
      + '<td>'
        + buildSparkline(
            (backup.recentRuns && backup.recentRuns.length > 0) ? backup.recentRuns : backup.recentBackups,
            backup.namespace, backup.name)
        + buildHistoryStats(backup) + '</td>'
      + '<td><span class="' + msgClass + '" title="' + msgText + '">' + msgText + '</span></td>'
      + '<td><div class="actions-cell">' + triggerBtn + pauseBtn + logsBtn + '</div></td>'
      + '</tr>';
  }

  function sortableTh(key, label) {
    return '<th data-key="' + key + '" onclick="toggleSort(\'' + key + '\')"'
      + ' title="Sort by ' + label.toLowerCase() + '">'
      + label + '<span class="sort-ind"></span></th>';
  }

  function buildTable(backups) {
    const rows = backups.map(buildRow).join('');
    // Identical colgroup in every namespace table — combined with
    // table-layout:fixed this keeps all sections' columns pixel-aligned.
    const widths = ['17%','10%','13%','12%','10%','10%','18%','10%'];
    let colgroup = '<colgroup>';
    for (const w of widths) colgroup += '<col style="width:' + w + '">';
    colgroup += '</colgroup>';
    return '<table class="backup-table">'
      + colgroup
      + '<thead><tr>'
      + sortableTh('name', 'Name')
      + sortableTh('status', 'Status')
      + sortableTh('lastBackupTime', 'Last backup')
      + sortableTh('nextBackupTime', 'Next backup')
      + '<th>Schedule</th>'
      + sortableTh('successRate', 'History')
      + '<th>Message</th>'
      + '<th></th>'
      + '</tr></thead>'
      + '<tbody>' + rows + '</tbody>'
      + '</table>';
  }

  // ── Filter + search helpers ─────────────────────────────────────────────────

  function filteredBackups() {
    let items = allBackups;

    if (activeFilter !== 'all') {
      items = items.filter(b => b.lastBackupResult === activeFilter);
    }

    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      items = items.filter(b =>
        (b.name || '').toLowerCase().includes(q) ||
        (b.namespace || '').toLowerCase().includes(q) ||
        (b.pvc || '').toLowerCase().includes(q)
      );
    }

    // Always hand out a copy: with no filter/search active this would return
    // allBackups itself, and render()'s in-place .sort() would permanently
    // scramble the API's ordering (the "default" order after clearSort).
    return items.slice();
  }

  // ── Render ──────────────────────────────────────────────────────────────────

  function render() {
    const visible = filteredBackups();
    if (sortKey) visible.sort(cmpBackups(sortKey));

    // Section header
    const filterLabel = activeFilter === 'all' ? 'All backups'
      : activeFilter === 'success' ? 'Healthy backups'
      : activeFilter === 'failed' ? 'Failed backups'
      : activeFilter === 'queued' ? 'Queued backups'
      : 'Running backups';
    document.getElementById('section-title').textContent = filterLabel;
    document.getElementById('section-count').textContent = visible.length;

    // Group by namespace, sorted alphabetically
    const byNs = {};
    for (const b of visible) {
      const ns = b.namespace || 'default';
      if (!byNs[ns]) byNs[ns] = [];
      byNs[ns].push(b);
    }
    const namespaces = Object.keys(byNs).sort();

    const container = document.getElementById('tables-container');

    if (visible.length === 0) {
      if (allBackups.length === 0) {
        container.innerHTML = '<div class="state-message">No backups found.</div>';
      } else {
        container.innerHTML = '<div class="state-message">No backups match the current filter.</div>';
      }
      return;
    }

    let html = '';
    for (const ns of namespaces) {
      html += '<div class="ns-divider">'
        + '<span class="ns-divider-label">' + escapeHtml(ns) + '</span>'
        + '<div class="ns-divider-line"></div>'
        + '</div>';
      html += buildTable(byNs[ns]);
    }
    container.innerHTML = html;
    setSortIndicators();
    updateSortChip();
  }

  function renderSidebar() {
    // Counts for views
    const total   = allBackups.length;
    const healthy = allBackups.filter(b => b.lastBackupResult === 'success').length;
    const failed  = allBackups.filter(b => b.lastBackupResult === 'failed').length;
    const running = allBackups.filter(b => b.lastBackupResult === 'running').length;
    const queued  = allBackups.filter(b => b.lastBackupResult === 'queued').length;

    document.getElementById('badge-all').textContent     = total;
    document.getElementById('badge-success').textContent = healthy;
    document.getElementById('badge-failed').textContent  = failed;
    document.getElementById('badge-running').textContent = running;
    document.getElementById('badge-queued').textContent  = queued;

    // Stat cards
    document.getElementById('stat-healthy').textContent = healthy;
    document.getElementById('stat-failed').textContent  = failed;
    document.getElementById('stat-running').textContent = running;
    document.getElementById('stat-queued').textContent  = queued;
    const pending = allBackups.filter(b => b.lastBackupResult === 'pending' || !b.lastBackupResult).length;
    document.getElementById('stat-pending').textContent = pending;

    // Namespace list in sidebar
    const nsCounts = {};
    for (const b of allBackups) {
      const ns = b.namespace || 'default';
      nsCounts[ns] = (nsCounts[ns] || 0) + 1;
    }
    const nsNames = Object.keys(nsCounts).sort();
    let nsHtml = '';
    for (const ns of nsNames) {
      nsHtml += '<div class="sidebar-item">'
        + '<span class="dot"></span> ' + escapeHtml(ns)
        + '<span class="sidebar-badge">' + nsCounts[ns] + '</span>'
        + '</div>';
    }
    document.getElementById('sidebar-namespaces').innerHTML = nsHtml;

    // Footer
    const nsCount = nsNames.length;
    document.getElementById('footer-summary').textContent =
      total + ' backup' + (total !== 1 ? 's' : '')
      + ' across ' + nsCount + ' namespace' + (nsCount !== 1 ? 's' : '');
  }

  function renderRefreshStatus(isError) {
    const el = document.getElementById('refresh-status');
    if (isError) {
      el.textContent = 'Connection error — retrying…';
      el.className = 'topbar-refresh error';
    } else {
      el.textContent = refreshLabel(secondsSinceLastFetch());
      el.className = 'topbar-refresh';
    }
  }

  // ── Sidebar filter ──────────────────────────────────────────────────────────

  function setFilter(filter) {
    activeFilter = filter;

    // Update active state on sidebar items
    document.querySelectorAll('.sidebar-item[data-filter]').forEach(function(el) {
      if (el.dataset.filter === filter) {
        el.classList.add('active');
      } else {
        el.classList.remove('active');
      }
    });

    render();
  }

  // ── Search ──────────────────────────────────────────────────────────────────

  function applySearch() {
    searchQuery = document.getElementById('search-input').value.trim();
    render();
  }

  // ── Data fetching ───────────────────────────────────────────────────────────

  let fetchError = false;

  function loadBackups() {
    fetch('/api/backups')
      .then(function(response) {
        if (!response.ok) {
          throw new Error('HTTP ' + response.status);
        }
        return response.json();
      })
      .then(function(data) {
        fetchError = false;
        allBackups = Array.isArray(data) ? data : [];
        lastFetchTime = Date.now();
        renderSidebar();
        render();
        renderRefreshStatus(false);
        scheduleFastPoll();
      })
      .catch(function() {
        fetchError = true;
        renderRefreshStatus(true);
      });
  }

  function scheduleFastPoll() {
    if (fastPollTimer) return; // already scheduled
    var hasActive = allBackups.some(function(b) {
      return b.lastBackupResult === 'running' || b.lastBackupResult === 'queued' || b.triggeredAt;
    }) || Object.keys(activeRunState).length > 0;
    if (hasActive) {
      fastPollTimer = setTimeout(function() {
        fastPollTimer = null;
        loadBackups();
      }, 3000);
    }
  }

  // ── Refresh counter tick ────────────────────────────────────────────────────

  function startRefreshCounter() {
    if (refreshCounterInterval) clearInterval(refreshCounterInterval);
    refreshCounterInterval = setInterval(function() {
      if (!fetchError) {
        renderRefreshStatus(false);
      }
    }, 5000);
  }

  // Mutating API calls carry the optional dashboard token (K8SI_UI_TOKEN on
  // the server): stored locally after a one-time prompt, retried once on 401.
  function apiFetch(url, opts) {
    opts = opts || {};
    var token = localStorage.getItem('k8si-ui-token');
    if (token) {
      opts.headers = Object.assign({}, opts.headers, {'X-K8si-Token': token});
    }
    return fetch(url, opts).then(function(resp) {
      if (resp.status === 401) {
        var t = window.prompt('Dashboard token required (set K8SI_UI_TOKEN on the k8si-ui deployment):');
        if (t) {
          localStorage.setItem('k8si-ui-token', t);
          opts.headers = Object.assign({}, opts.headers, {'X-K8si-Token': t});
          return fetch(url, opts);
        }
      }
      return resp;
    });
  }

  // ── Backup trigger ──────────────────────────────────────────────────────────

  // Icon-only buttons: runtime state is expressed through classes (color +
  // pulse) and the title tooltip — never textContent, which would wipe the SVG.
  function setBtnState(btn, state, title) {
    btn.classList.remove('queued', 'running', 'error');
    if (state) btn.classList.add(state);
    if (title) btn.title = title;
  }

  var toastTimer = null;
  function showToast(message) {
    var el = document.getElementById('toast');
    if (!el) return;
    el.textContent = message;
    el.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function() { el.classList.remove('show'); }, 4000);
  }

  function triggerBackup(namespace, name, btn) {
    // Immediate click feedback only — after the first render() this button is
    // detached (fast-poll re-renders every 3s); from there activeRunState +
    // buildRow drive the fresh button, and failures surface via showToast.
    btn.disabled = true;
    setBtnState(btn, 'queued', 'Queued…');
    var runKey = namespace + '/' + name;
    activeRunState[runKey] = {runName: null, phase: 'queued'};
    render();

    apiFetch('/api/backups/' + encodeURIComponent(namespace) + '/' + encodeURIComponent(name) + '/trigger', {
      method: 'POST',
    })
      .then(function(resp) {
        if (!resp.ok) {
          return resp.json().catch(function() { return {}; }).then(function(body) {
            throw new Error(body.detail || ('HTTP ' + resp.status));
          });
        }
        return resp.json();
      })
      .then(function(data) {
        activeRunState[runKey] = {runName: data.runName, phase: 'queued'};
        render();
        loadBackups(); // refresh counter tiles immediately — backend just set lastBackupResult=running
        openRunLogs(namespace, data.runName, name, [], function(phase) {
          // SSE phase callback: data only — the freshly rendered button picks
          // the phase up from activeRunState via buildRow.
          if (phase === 'running') {
            activeRunState[runKey].phase = 'running';
            render();
          } else if (phase === 'done') {
            delete activeRunState[runKey];
            render();
            loadBackups();
          }
        });
      })
      .catch(function(err) {
        delete activeRunState[runKey];
        render();
        showToast('Backup failed: ' + (err.message || 'error'));
      });
  }

  // ── Pause / resume ──────────────────────────────────────────────────────────

  function setPaused(namespace, name, paused, btn) {
    btn.disabled = true;
    setBtnState(btn, 'queued', paused ? 'Pausing…' : 'Resuming…');

    apiFetch('/api/backups/' + encodeURIComponent(namespace) + '/' + encodeURIComponent(name) + '/paused', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paused: paused}),
    })
      .then(function(resp) {
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        // Refresh the full data so the row re-renders with the new paused state
        loadBackups();
      })
      .catch(function() {
        showToast((paused ? 'Pause' : 'Resume') + ' failed — retrying via refresh');
        loadBackups();
      });
  }

  // ── Log drawer & Tab management ─────────────────────────────────────────────

  let logTabs = [];
  let activeTabId = null;

  function setDrawerStatus(dotClass, label) {
    var dot = document.getElementById('drawer-status-dot');
    if (dot) dot.className = 'drawer-status-dot' + (dotClass ? ' ' + dotClass : '');
    var statusEl = document.getElementById('log-drawer-status');
    if (statusEl) statusEl.textContent = label;
  }

  function formatDrawerTime(isoString) {
    if (!isoString) return '—';
    var d = new Date(isoString);
    var day = String(d.getDate()).padStart(2, '0');
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var h = String(d.getHours()).padStart(2, '0');
    var m = String(d.getMinutes()).padStart(2, '0');
    var s = String(d.getSeconds()).padStart(2, '0');
    return day + ' ' + months[d.getMonth()] + ' ' + h + ':' + m + ':' + s;
  }

  function durationStr(startIso, endIso) {
    if (!startIso || !endIso) return null;
    var secs = Math.round((new Date(endIso) - new Date(startIso)) / 1000);
    if (secs < 60) return secs + 's';
    var m = Math.floor(secs / 60), s = secs % 60;
    return s > 0 ? m + 'm ' + s + 's' : m + 'm';
  }

  function formatSize(bytes) {
    if (bytes === null || bytes === undefined) return null;
    if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GiB';
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MiB';
    if (bytes >= 1024) return (bytes / 1024).toFixed(1) + ' KiB';
    return bytes + ' B';
  }

  function getHistoricalRuns(ns, nm) {
    var backup = allBackups.find(function(b) { return b.namespace === ns && b.name === nm; });
    if (!backup || !backup.recentRuns) return [];
    return backup.recentRuns.filter(function(r) { return r.result !== 'running'; });
  }

  function openRunLogsWithPicker(ns, nm, runName) {
    var historicalRuns = getHistoricalRuns(ns, nm);
    openRunLogs(ns, runName, nm, historicalRuns, null);
  }

  function openBackupLogs(ns, nm) {
    var historicalRuns = getHistoricalRuns(ns, nm);
    var backup = allBackups.find(function(b) { return b.namespace === ns && b.name === nm; });

    // A live run always wins over stale completed history.
    var live = activeRunState[ns + '/' + nm];
    if (live && live.runName) {
      openRunLogs(ns, live.runName, nm, historicalRuns, null);
      return;
    }
    if (backup && backup.lastBackupResult === 'running' && backup.lastRunRef) {
      openRunLogs(ns, backup.lastRunRef, nm, historicalRuns, null);
      return;
    }
    var latestRun = historicalRuns[0];
    if (latestRun) {
      openRunLogs(ns, latestRun.name, nm, historicalRuns, null);
      return;
    }
    if (backup && backup.lastRunRef) {
      openRunLogs(ns, backup.lastRunRef, nm, historicalRuns, null);
      return;
    }
    var btn = document.querySelector('.btn-logs[data-ns="' + ns + '"][data-nm="' + nm + '"]');
    if (btn) {
      btn.textContent = 'No runs yet';
      setTimeout(function() { btn.textContent = 'Logs'; }, 3000);
    }
  }

  function onRunPickerChange(sel) {
    var runName = sel.value;
    var ns = sel.dataset.namespace;
    var nm = sel.dataset.backupName;
    if (!ns || !nm) return;
    var historicalRuns = getHistoricalRuns(ns, nm);
    if (activeTabId) {
      closeTab(activeTabId);
    }
    openRunLogs(ns, runName, nm, historicalRuns, null);
  }

  function renderTabs() {
    var tabsBar = document.getElementById('log-tabs-bar');
    if (!tabsBar) return;
    tabsBar.innerHTML = '';

    logTabs.forEach(function(tab) {
      var tabEl = document.createElement('div');
      tabEl.className = 'log-tab' + (tab.id === activeTabId ? ' active' : '');
      tabEl.onclick = function(e) {
        if (e.target.classList.contains('log-tab-close')) return;
        activateTab(tab.id);
      };

      var dotEl = document.createElement('span');
      dotEl.className = 'log-tab-dot' + (tab.statusDot ? ' ' + tab.statusDot : '');

      var titleEl = document.createElement('span');
      titleEl.className = 'log-tab-title';
      titleEl.textContent = tab.title;
      titleEl.title = tab.title;

      var closeEl = document.createElement('span');
      closeEl.className = 'log-tab-close';
      closeEl.textContent = '×';
      closeEl.title = 'Close tab';
      closeEl.onclick = function(e) {
        e.stopPropagation();
        closeTab(tab.id);
      };

      tabEl.appendChild(dotEl);
      tabEl.appendChild(titleEl);
      tabEl.appendChild(closeEl);
      tabsBar.appendChild(tabEl);
    });
  }

  function updateTabHeaderAndDot(tab) {
    if (!tab || tab.id !== activeTabId) return;

    setDrawerStatus(tab.statusDot, tab.statusText);

    var title = document.getElementById('log-drawer-title');
    if (title) title.textContent = tab.title;

    var picker = document.getElementById('log-run-picker');
    if (!picker) return;

    if (tab.recentRuns && tab.recentRuns.length > 1) {
      picker.style.display = '';
      picker.dataset.namespace = tab.namespace;
      picker.dataset.backupName = tab.backupName;
      picker.innerHTML = '';
      tab.recentRuns.forEach(function(run) {
        var opt = document.createElement('option');
        opt.value = run.name;
        var label = run.result + ' · ' + formatDateTime(run.time);
        if (run.sizeBytes != null) label += ' · ' + formatSize(run.sizeBytes);
        opt.textContent = label;
        opt.selected = (run.name === tab.targetName);
        picker.appendChild(opt);
      });
    } else {
      picker.style.display = 'none';
      picker.innerHTML = '';
    }
  }

  function activateTab(tabId) {
    activeTabId = tabId;
    var activeTab = logTabs.find(function(t) { return t.id === tabId; });
    if (!activeTab) return;

    logTabs.forEach(function(t) {
      if (t.contentEl) t.contentEl.style.display = (t.id === tabId) ? 'block' : 'none';
    });

    renderTabs();
    updateTabHeaderAndDot(activeTab);

    document.querySelectorAll('.btn-logs').forEach(function(b) { b.classList.remove('active'); });
    var activeBtn = document.querySelector('.btn-logs[data-ns="' + activeTab.namespace + '"][data-nm="' + activeTab.backupName + '"]');
    if (activeBtn) activeBtn.classList.add('active');
  }

  function releaseActiveRunForTab(tab) {
    // The only other cleanup for an activeRunState entry is the 'done' SSE
    // message — delivered over the stream this tab owns. When the tab (or its
    // stream) dies, release the entry here or it strands forever: buildRow
    // prefers it over polled data (frozen row) and scheduleFastPoll re-arms
    // the 3s poll while it exists.
    if (!tab || !tab.isRunLog) return false;
    var st = activeRunState[tab.namespace + '/' + tab.backupName];
    if (st && st.runName === tab.targetName) {
      delete activeRunState[tab.namespace + '/' + tab.backupName];
      return true;
    }
    return false;
  }

  function openRunLogs(namespace, runName, backupName, recentRuns, phaseCallback) {
    var tabId = namespace + '/' + runName;
    var existingTab = logTabs.find(function(t) { return t.id === tabId; });

    if (existingTab) {
      if (phaseCallback) existingTab.phaseCallback = phaseCallback;
      if (recentRuns) existingTab.recentRuns = recentRuns;
      if (backupName) existingTab.backupName = backupName;
      activateTab(tabId);
      return;
    }

    while (logTabs.length >= MAX_LOG_TABS) {
      closeTab(logTabs[0].id);
    }

    var drawer = document.getElementById('log-drawer');
    drawer.style.display = 'flex';

    var container = document.getElementById('log-content-container');
    var contentEl = document.createElement('div');
    contentEl.className = 'log-content';
    contentEl.id = 'log-content-' + tabId.replace(/[^a-zA-Z0-9_-]/g, '_');
    contentEl.style.display = 'none';
    container.appendChild(contentEl);

    var tab = {
      id: tabId,
      namespace: namespace,
      targetName: runName,
      backupName: backupName || runName,
      isRunLog: true,
      title: namespace + '/' + runName,
      eventSource: null,
      statusDot: '',
      statusText: 'connecting…',
      recentRuns: recentRuns,
      phaseCallback: phaseCallback,
      contentEl: contentEl
    };
    logTabs.push(tab);

    activateTab(tabId);

    var url = '/api/runs/' + encodeURIComponent(namespace) + '/' + encodeURIComponent(runName) + '/logs';
    var es = new EventSource(url);
    tab.eventSource = es;

    es.onopen = function() {
      tab.statusDot = 'live';
      tab.statusText = 'live';
      updateTabHeaderAndDot(tab);
      renderTabs();
    };

    es.onmessage = function(e) {
      var data = JSON.parse(e.data);

      if (data.type === 'phase') {
        var prev = contentEl.querySelector('.log-phase.active');
        if (prev) prev.className = 'log-phase';
        var oldBar = contentEl.querySelector('.log-activity-bar');
        if (oldBar) oldBar.remove();

        var t = data.time
          ? new Date(data.time).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false})
          : '';
        var line = document.createElement('div');
        line.className = 'log-phase active';
        line.innerHTML = '<span class="phase-time">' + escapeHtml(t) + '</span>'
          + '<span class="phase-name">' + escapeHtml(data.phase || '') + '</span>'
          + '<span>' + escapeHtml(data.message || '') + '</span>';
        contentEl.appendChild(line);

        var bar = document.createElement('div');
        bar.className = 'log-activity-bar';
        contentEl.appendChild(bar);

        if (tab.phaseCallback) tab.phaseCallback('running');

      } else if (data.type === 'error') {
        tab.statusDot = 'failed';
        tab.statusText = 'error';
        updateTabHeaderAndDot(tab);
        renderTabs();
        var errEl = document.createElement('div');
        errEl.className = 'log-line';
        errEl.style.color = '#f85149';
        errEl.textContent = data.message || 'stream error';
        contentEl.appendChild(errEl);
        es.close();
        tab.eventSource = null;

      } else if (data.type === 'done') {
        var lastBar = contentEl.querySelector('.log-activity-bar');
        if (lastBar) lastBar.remove();
        var lastActive = contentEl.querySelector('.log-phase.active');
        if (lastActive) lastActive.className = 'log-phase';

        var summary = document.createElement('div');
        if (data.result === 'timeout') {
          tab.statusDot = '';
          tab.statusText = 'stream closed';
          updateTabHeaderAndDot(tab);
          renderTabs();
          summary.className = 'log-summary';
          summary.innerHTML = '<div class="log-summary-title">Log stream closed</div>'
            + '<div class="log-summary-grid">'
            + '<span class="summary-key">Status</span><span class="summary-val">Backup still running</span>'
            + '</div>';
          contentEl.appendChild(summary);
          es.close();
          tab.eventSource = null;
        } else {
          var isSuccess = data.result === 'success';
          tab.statusDot = isSuccess ? 'done' : 'failed';
          tab.statusText = isSuccess ? 'completed' : 'failed';
          updateTabHeaderAndDot(tab);
          renderTabs();
          summary.className = 'log-summary' + (isSuccess ? '' : ' failed');
          var dur = durationStr(data.startTime, data.completionTime);
          var snapShort = data.snapshotId ? data.snapshotId.substring(0, 8) : null;
          var sizeStr = data.sizeBytes != null ? formatSize(data.sizeBytes) : null;
          var bk = allBackups.find(function(b) {
            return b.namespace === namespace && b.name === tab.backupName;
          });
          var destination = bk ? (bk.backupSecret || bk.resticSecret || null) : null;
          summary.innerHTML = '<div class="log-summary-title">'
            + (isSuccess ? 'Backup completed successfully' : 'Backup failed') + '</div>'
            + '<div class="log-summary-grid">'
            + '<span class="summary-key">' + (isSuccess ? 'Finished' : 'Failed at') + '</span>'
            + '<span class="summary-val">' + escapeHtml(formatDrawerTime(data.completionTime)) + '</span>'
            + (dur ? '<span class="summary-key">Duration</span><span class="summary-val">' + escapeHtml(dur) + '</span>' : '')
            + '<span class="summary-key">Run</span><span class="summary-val">' + escapeHtml(namespace + '/' + runName) + '</span>'
            + (snapShort ? '<span class="summary-key">Snapshot</span><span class="summary-val" title="' + escapeHtml(data.snapshotId) + '" style="font-family:monospace">' + escapeHtml(snapShort) + '</span>' : '')
            + (sizeStr ? '<span class="summary-key">Size</span><span class="summary-val">' + escapeHtml(sizeStr) + '</span>' : '')
            + (destination ? '<span class="summary-key">Destination</span><span class="summary-val">' + escapeHtml(destination) + '</span>' : '')
            + (data.message ? '<span class="summary-key">Message</span><span class="summary-val">' + escapeHtml(data.message) + '</span>' : '')
            + '</div>';
          contentEl.appendChild(summary);
          es.close();
          tab.eventSource = null;
          if (tab.phaseCallback) tab.phaseCallback('done');
        }
      }

      contentEl.scrollTop = contentEl.scrollHeight;
    };

    es.onerror = function() {
      tab.statusDot = '';
      tab.statusText = 'disconnected';
      updateTabHeaderAndDot(tab);
      renderTabs();
      // The stream died without a 'done' — release the run state so the row
      // follows polled truth instead of freezing on the last phase.
      if (releaseActiveRunForTab(tab)) render();
      es.close();
      tab.eventSource = null;
    };
  }


  function closeTab(tabId) {
    var index = logTabs.findIndex(function(t) { return t.id === tabId; });
    if (index === -1) return;

    var tab = logTabs[index];
    if (tab.eventSource) {
      tab.eventSource.close();
      tab.eventSource = null;
    }
    if (tab.contentEl) {
      tab.contentEl.remove();
    }

    logTabs.splice(index, 1);

    // The tab owned the stream that would have delivered the 'done' cleanup —
    // closing it must also release the optimistic run state, or the row badge
    // freezes on the stale phase and the 3s fast-poll never stops (#14).
    if (releaseActiveRunForTab(tab)) render();

    if (logTabs.length === 0) {
      activeTabId = null;
      document.getElementById('log-drawer').style.display = 'none';
      document.querySelectorAll('.btn-logs').forEach(function(b) { b.classList.remove('active'); });
    } else {
      var nextTab = logTabs[Math.min(index, logTabs.length - 1)];
      activateTab(nextTab.id);
    }
  }

  function closeLogs() {
    if (activeTabId) {
      closeTab(activeTabId);
    } else {
      closeAllLogs();
    }
  }

  function closeAllLogs() {
    logTabs.forEach(function(tab) {
      if (tab.eventSource) tab.eventSource.close();
      if (tab.contentEl) tab.contentEl.remove();
    });
    logTabs = [];
    activeTabId = null;
    document.getElementById('log-drawer').style.display = 'none';
    document.querySelectorAll('.btn-logs').forEach(function(b) { b.classList.remove('active'); });
  }

  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      var drawer = document.getElementById('log-drawer');
      if (drawer && drawer.style.display !== 'none') closeLogs();
    }
  });

  // ── Bootstrap ───────────────────────────────────────────────────────────────

  loadBackups();
  setInterval(loadBackups, 30000);
  startRefreshCounter();

  fetch('/api/version')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.version) {
        document.getElementById('footer-version').textContent = ' ' + d.version;
      }
    })
    .catch(function() {});

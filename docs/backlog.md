# k8si backlog

Items discovered during v0.8.0 development and production validation.

---

## 1. K8siBackupRun reconciliation timer

**Problem:** `on_run_create` is purely event-driven. If the operator restarts while a run is
Pending/Running, the watch event is never re-delivered and the run stays stuck forever. This
is exactly what happened in production: a run was created while the operator was down, came back
up, never fired `on_run_create`, run sat Pending indefinitely.

**Fix:** Add a `@kopf.timer` on `k8sibackupruns` (e.g. every 60s) that picks up any run in
Pending/Running state that has been there longer than a threshold and either kicks it or marks
it Failed. Same self-healing pattern the `backup_timer` already uses for K8siBackup.

---

## 2. Operator startup health check

**Problem:** Operator starts successfully even when required CRDs or RBAC are missing, then
silently fails when it tries to use them. No user-visible indication of a broken deployment.

**Fix:** In `@kopf.on.startup`, check:
- `k8sibackupruns.k8si.io` CRD exists
- Operator SA can create/patch `k8sibackupruns` in all relevant namespaces
- UI SA can list/create `k8sibackupruns`

If any check fails: log a clear ERROR, set a startup condition on the operator, and optionally
surface it in the UI (`/healthz` could return degraded status with a reason).

---

## 3. `k8si` CLI install tool

**Problem:** Deploying k8si requires manually applying multiple files in the right order
(`crd.yaml`, `crd_run.yaml`, `rbac.yaml`, `operator.yaml`, `ui.yaml`). Homelab infra repos
(e.g. ArgoCD manifests) can drift from the authoritative source, causing silent mismatches.

**Fix:** A `k8si` CLI that generates versioned install manifests:

```bash
k8si generate install              # outputs all YAML for current version
k8si generate install | kubectl apply -f -
k8si validate                      # checks CRDs present, RBAC correct, operator running
```

Same pattern as `linkerd install`, `argocd install`. The CLI knows its own version and emits
the exact matching manifests. Eliminates homelab drift. Operator needs no elevated RBAC.

---

## 4. UX: backup trigger flow

**Current state (bad):**
- Clicking "Backup now" shows "Failed" if anything goes wrong (RBAC, CRD missing, 409) — no detail
- Status column stays "success" while a run is queued
- Log drawer opens but shows no progress indicator
- Drawer closes on completion, losing the result
- Failed backups show no way to inspect what went wrong

---

### 4.1 Button state machine

The "Backup now" button has four visual states. Transitions are triggered by API responses and
SSE events, never by timers or guesses.

```
                     click
  [idle] ──────────────────────> [queued]
    ^                               │
    │                          SSE phase="Running"
    │                               │
    │                               v
    │                          [running]
    │                               │
    │                          SSE done (success or failed)
    │                               │
    ├───────────────────────────────┘
    │
    │    POST returns HTTP error
    │<──────── [error] (auto-resets after 4s)
```

| State | Label | Style | Disabled | Notes |
|-------|-------|-------|----------|-------|
| **idle** | `Backup now` | default | no | Normal resting state |
| **queued** | `Queued...` | amber border/text, pulsing dot | yes | Set immediately on click, before POST returns |
| **running** | `Running...` | blue border/text, pulsing dot | yes | Set when SSE delivers first `phase` event with run phase=Running |
| **error** | `Failed: {reason}` | red border/text | no | Show truncated reason from HTTP error body (e.g. "409: already running", "403: RBAC denied"). Auto-resets to idle after 4s. |
| **idle (after success)** | `Backup now` | default | no | Button resets silently; success lives in the status badge and drawer |

**Optimistic transition:** The button moves to `queued` the instant the user clicks, before the
POST completes. If the POST fails, it jumps to `error`, skipping `running` entirely.

---

### 4.2 Status column badge state machine

The status badge tracks the run phase during an active run rather than `lastBackupResult`
(which lags behind).

| Badge | Dot color | Animation | Clickable |
|-------|-----------|-----------|-----------|
| `success` | green `#3fb950` | none | no |
| `queued` | amber `#d29922` | pulse | no |
| `running` | blue `#58a6ff` | pulse | no |
| `failed` | red `#f85149` | none | **yes** — opens log drawer for the failed run |
| `pending` | grey `#6e7681` | none | no |

**Key rule:** While a K8siBackupRun exists in Pending or Running state, the badge shows
`queued` or `running`, overriding `lastBackupResult`. It returns to `lastBackupResult` only
after the run reaches a terminal state.

---

### 4.3 Log drawer: Claude Code CLI style

"Claude Code CLI style" means: dark terminal aesthetic, monospace text flowing downward like a
build log, with a left-edge activity rail that makes it immediately obvious whether work is
happening — like watching a CI pipeline or `kubectl logs -f`, not a static text box.

#### Drawer anatomy

```
┌─────────────────────────────────────────────────────────────────┐
│  mariadb-prod / run-20260614-1423   ● live            [Close]  │  ← header
├─────────────────────────────────────────────────────────────────┤
│ ┃ 14:23:01  QuiesceStarted     Acquiring FTWRL lock...         │  ← completed phase
│ ┃ 14:23:02  HookStarted        Running pre-snapshot hook       │
│ ▍ 14:23:04  SnapshotStarted    Creating VolumeSnapshot...      │  ← active phase (bar pulses)
│ ▍                                                              │
│ ▍ ░░░░░░░░░░░░░                                                │  ← shimmer activity bar
└─────────────────────────────────────────────────────────────────┘
```

#### Activity rail (left border)

Every phase line has a 2px left border. Completed phases: solid dim (`#30363d`). Active phase
(last received before `done`): solid blue (`#58a6ff`) with pulse animation. This is the primary
"something is happening" signal.

#### Activity indicator

Below the last phase line, while active, a shimmer bar animates (CSS gradient sliding left to
right). Removed when `done` event arrives.

```css
@keyframes shimmer {
  0%   { background-position: -200px 0; }
  100% { background-position: 200px 0; }
}
.log-activity-bar {
  height: 3px;
  margin: 6px 0 6px 8px;
  width: 120px;
  border-radius: 2px;
  background: linear-gradient(90deg, #21262d 0%, #58a6ff 50%, #21262d 100%);
  background-size: 200px 3px;
  animation: shimmer 1.5s infinite linear;
}
```

#### Drawer header

| Run state | Dot | Label |
|-----------|-----|-------|
| Connecting | dim grey | `connecting...` |
| Live (Pending/Running) | blue pulsing | `live` |
| Success | green solid | `completed` |
| Failed | red solid | `failed` |

#### Phase-by-phase rendering

Each SSE `phase` event appends a log line. When a new phase arrives, the previous phase's
border dims and stops pulsing. The shimmer bar moves below the new active phase.

#### Terminal state: success

```
───────────────────────────────────────────────
  Backup completed successfully

  Finished     14 Jun 14:27:31
  Duration     4m 27s
  Snapshot     mariadb-prod/run-20260614-1423
  Phases       6 completed
───────────────────────────────────────────────
```

Green-tinted card (`#0d2818`, border `#1a4428`). Shimmer bar removed. Drawer stays open.

Fields: `Finished` (completionTime), `Duration` (completionTime − startTime), `Snapshot`
(namespace/runName), `Phases` (count of phase events received).

#### Terminal state: failed

Same card layout, red-tinted (`#2d0f0f`, border `#4a1515`):

```
───────────────────────────────────────────────
  Backup failed

  Failed at    14 Jun 14:25:12
  Duration     1m 38s
  Last phase   SnapshotFailed
  Error        VolumeSnapshot timed out after 120s
───────────────────────────────────────────────
```

`Error` field: `message` from the `done` event or last phase's message.

---

### 4.4 Failed backup: click-to-view-logs

When the status badge shows `failed`, clicking it opens the log drawer for the **most recent
K8siBackupRun**. The run is already terminal, so the SSE endpoint returns all stored phases
immediately then sends `done` — no live polling needed.

**Requirements:**
- `failed` badge: `cursor: pointer`, hover underline
- Click calls `openRunLogs(namespace, lastRunName)`
- `GET /api/backups` response must include `lastRunName` per backup (add to `_shape()`)

---

### 4.5 Implementation checklist

| Area | Change |
|------|--------|
| Button | Add `queued` and `error` states. Show HTTP error detail (not just "Failed"). |
| Status badge | Override with run phase while active. Make `failed` badge clickable. |
| Drawer header | Add colored status dot (pulsing live, solid on terminal). |
| Drawer body | Activity rail (pulsing left border on active phase). Shimmer bar below last active phase. |
| Drawer terminal | Replace text line with summary card: timestamp, duration, snapshot ID, phase count / error. |
| Drawer lifecycle | Never auto-close. Close on button click or Escape key. |
| CSS | Add `@keyframes shimmer`, `.log-activity-bar`, `.log-summary`, `.log-summary.failed`, `cursor: pointer` on clickable badge. |
| API `_shape()` | Add `lastRunName` field. |
| SSE endpoint | For terminal runs, emit all stored phases then `done` immediately (no polling loop). |

---

## 5. Deployment gap: CRD + RBAC not applied on upgrade

**What happened in v0.8.0:** `crd_run.yaml` was added but not applied to production because
homelab2 ArgoCD manifests weren't updated. UI SA was also missing `k8sibackupruns` RBAC.
Operator started fine but silently failed on every trigger.

**Root cause:** `deploy/` in the k8si repo is the authoritative source, but nothing enforces
that external infra repos stay in sync.

**Fix options (in order of preference):**
- CLI tool (see item 3) — makes the authoritative source executable
- e2e test that validates installed CRDs and RBAC against `deploy/` manifests
- Document upgrade steps explicitly in release notes for each version that adds CRDs/RBAC

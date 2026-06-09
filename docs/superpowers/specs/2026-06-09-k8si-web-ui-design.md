# k8si Web UI — Design Spec

## Goal

A read-only dashboard showing all `K8siBackup` resources across namespaces: current status, timing, and a rolling backup history. Inspired by Duplicati's clean, dark-themed interface.

---

## Architecture

```
┌─────────────────────────┐        ┌──────────────────────────────┐
│   k8si-ui pod           │        │  Kubernetes API              │
│                         │        │                              │
│  FastAPI app            │◄──────►│  k8sibackups (all ns)        │
│  - GET /                │  RBAC  │  (spec + status incl.        │
│  - GET /api/backups     │        │   recentBackups)             │
└────────────┬────────────┘        └──────────────────────────────┘
             │ NodePort :30080
             ▼
         Browser (homelab)
```

**Separate pod** — independent of the operator. Has its own ServiceAccount with minimal read-only RBAC. Exposed via a `NodePort` Service on port 30080.

---

## CRD Status Extension

The operator gains a new status field written after every backup run:

```yaml
status:
  lastBackupTime: "2026-06-09T02:01:00Z"
  lastBackupResult: success
  nextBackupTime: "2026-06-10T02:00:00Z"
  message: ""
  recentBackups:           # NEW — rolling list, max 30 entries
    - time: "2026-06-09T02:01:00Z"
      result: success
    - time: "2026-06-08T02:01:00Z"
      result: success
    - time: "2026-06-07T02:01:00Z"
      result: failed
```

The operator appends to the front of `recentBackups` and trims to 30 after each completed or failed run. The field is absent on brand-new resources.

---

## UI Pod

**Image**: `ghcr.io/jaccoh/k8si-ui:latest` (built from `k8si/ui/`)

**Runtime**: Python 3.14, FastAPI + uvicorn, kubernetes-client

**Endpoints**:

| Path | Description |
|---|---|
| `GET /` | Serves `dashboard.html` (embedded) |
| `GET /api/backups` | Returns all K8siBackup resources as JSON |

`/api/backups` response shape:
```json
[
  {
    "name": "sonarr-config",
    "namespace": "downloads",
    "pvc": "sonarr-config-pvc",
    "schedule": "0 2 * * *",
    "lastBackupTime": "2026-06-09T02:01:00Z",
    "lastBackupResult": "success",
    "nextBackupTime": "2026-06-10T02:00:00Z",
    "message": "",
    "recentBackups": [
      {"time": "2026-06-09T02:01:00Z", "result": "success"},
      ...
    ]
  }
]
```

**Auto-refresh**: dashboard polls `/api/backups` every 30 seconds via `setInterval`.

---

## Dashboard Layout

Matches the approved mockup:

- **Top bar** — k8si branding, cluster name (from `CLUSTER_NAME` env var, default `"k8s"`), last-updated timestamp
- **Sidebar** — All / Healthy / Failed / Running, plus per-namespace filters
- **Summary strip** — 4 stat cards: Healthy / Failed / Running / Pending
- **Per-namespace tables** — Name, Status badge, Last backup (+ relative time), Next backup, Schedule, **Sparkline** (7 bars from `recentBackups`), Message
- **Status badges** — green (success), red (failed), animated blue (running), grey (pending)
- **Sparklines** — last 7 `recentBackups` entries rendered as coloured bars

---

## Kubernetes Resources

```yaml
# ServiceAccount
apiVersion: v1
kind: ServiceAccount
metadata:
  name: k8si-ui
  namespace: k8si

# ClusterRole — read-only on K8siBackup CRD
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: k8si-ui
rules:
  - apiGroups: ["k8si.jaccoh.com"]
    resources: ["k8sibackups"]
    verbs: ["get", "list", "watch"]

# ClusterRoleBinding
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: k8si-ui
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: k8si-ui
subjects:
  - kind: ServiceAccount
    name: k8si-ui
    namespace: k8si

# Deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: k8si-ui
  namespace: k8si
spec:
  replicas: 1
  selector:
    matchLabels:
      app: k8si-ui
  template:
    metadata:
      labels:
        app: k8si-ui
    spec:
      serviceAccountName: k8si-ui
      containers:
        - name: ui
          image: ghcr.io/jaccoh/k8si-ui:latest
          ports:
            - containerPort: 8080
          env:
            - name: CLUSTER_NAME
              value: "homelab-prod"

# Service
apiVersion: v1
kind: Service
metadata:
  name: k8si-ui
  namespace: k8si
spec:
  type: NodePort
  selector:
    app: k8si-ui
  ports:
    - port: 8080
      targetPort: 8080
      nodePort: 30080
```

---

## File Layout

```
k8si/
  ui/
    app.py          # FastAPI app — /api/backups + static serve
    dashboard.html  # Single-file dashboard (inline CSS + JS)
    Dockerfile
  manifests/
    ui.yaml         # All K8s resources above in one file
```

---

## Operator Changes

- `k8si/operator/cronjob.py` — after each backup completes/fails, patch `status.recentBackups` (prepend + trim to 30)
- `k8si/operator/handler.py` (or equivalent restore path) — same patch on restore-triggered backup outcome

No CRD schema changes needed beyond adding the new status field to the OpenAPI spec in `config/crd.yaml`.

---

## Testing

- Unit tests for `/api/backups` response shape (mock K8s client)
- Unit test for `recentBackups` patch logic: prepend, trim at 30, first-run (field absent)
- UI: manual verification against the visual companion mockup

---

## Out of Scope

- Authentication / RBAC for the UI itself (homelab, trusted network)
- Write operations (trigger backup, restore)
- Prometheus/Grafana integration
- Dark/light theme toggle

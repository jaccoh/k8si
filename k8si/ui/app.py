"""k8si web UI — read-only backup status dashboard."""

import asyncio
import importlib.metadata
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import kubernetes
import kubernetes.client
import kubernetes.client.exceptions
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

GROUP = "k8si.io"
VERSION = "v1"
PLURAL = "k8sibackups"
RUN_PLURAL = "k8sibackupruns"


def _is_new_run(last_backup_time: str | None, since: str | None) -> bool:
    """Return True if last_backup_time is strictly after since (new run completed)."""
    if since is None:
        return True
    if not last_backup_time:
        return False
    try:
        return datetime.fromisoformat(last_backup_time) > datetime.fromisoformat(since)
    except (ValueError, TypeError):
        return True


def _parse_since(since: str | None) -> datetime | None:
    if since is None:
        return None
    try:
        return datetime.fromisoformat(since)
    except (ValueError, TypeError):
        return None


def _is_stale_entry(entry: dict, since_dt: datetime | None) -> bool:
    if since_dt is None:
        return False
    entry_time = entry.get("time")
    if not entry_time:
        return False
    try:
        return datetime.fromisoformat(entry_time) <= since_dt
    except (ValueError, TypeError):
        return False


def _load_k8s() -> None:
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    _load_k8s()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/api/backups")
def list_backups() -> list[dict[str, Any]]:
    custom = kubernetes.client.CustomObjectsApi()
    raw = custom.list_cluster_custom_object(GROUP, VERSION, PLURAL)
    return [_shape(item) for item in raw.get("items", [])]


def _compute_stats(recent: list[dict]) -> dict[str, Any]:
    if not recent:
        return {"successRate": None, "streak": 0}
    success_count = sum(1 for e in recent if e.get("result") == "success")
    success_rate = round(success_count / len(recent), 3)
    first_result = recent[0].get("result")
    if first_result not in ("success", "failed"):
        return {"successRate": success_rate, "streak": 0}
    streak = 0
    for entry in recent:
        if entry.get("result") == first_result:
            streak += 1
        else:
            break
    if first_result == "failed":
        streak = -streak
    return {"successRate": success_rate, "streak": streak}


def _shape(item: dict[str, Any]) -> dict[str, Any]:
    meta = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    recent = status.get("recentBackups", [])
    stats = _compute_stats(recent)
    return {
        "name": meta.get("name", ""),
        "namespace": meta.get("namespace", ""),
        "pvc": spec.get("pvc", ""),
        "schedule": spec.get("schedule", ""),
        "paused": spec.get("paused", False),
        "backupWindow": spec.get("backupWindow", {}),
        "lastBackupTime": status.get("lastBackupTime"),
        "lastBackupResult": status.get("lastBackupResult", "pending"),
        "nextBackupTime": status.get("nextBackupTime"),
        "triggeredAt": status.get("triggeredAt"),
        "lastRunRef": status.get("lastRunRef"),
        "message": status.get("message", ""),
        "recentBackups": recent,
        "successRate": stats["successRate"],
        "streak": stats["streak"],
        "lastBackupDuration": status.get("lastBackupDuration"),
        "lastRestoreResult": status.get("lastRestoreResult"),
        "lastRestoreTime": status.get("lastRestoreTime"),
        "lastRestoreMessage": status.get("lastRestoreMessage"),
    }


@app.post("/api/backups/{namespace}/{name}/trigger")
def trigger_backup(namespace: str, name: str) -> dict[str, Any]:
    custom = kubernetes.client.CustomObjectsApi()
    try:
        backup_obj = custom.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"{namespace}/{name} not found")
        raise HTTPException(status_code=500, detail=str(e))

    try:
        runs = custom.list_namespaced_custom_object(
            GROUP,
            VERSION,
            namespace,
            RUN_PLURAL,
            label_selector=f"{GROUP}/backup={name}",
        )
        for run in runs.get("items", []):
            phase = run.get("status", {}).get("phase", "Pending")
            if phase in ("Pending", "Running"):
                run_name_active = run["metadata"]["name"]
                raise HTTPException(
                    status_code=409,
                    detail=f"run {run_name_active} is already active (phase={phase})",
                )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    ts = datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S")
    run_name = f"{name}-{ts}"
    triggered_at = datetime.now(tz=UTC).isoformat()
    mode = backup_obj.get("spec", {}).get("backupMode", "snapshot")

    run_obj = {
        "apiVersion": f"{GROUP}/v1",
        "kind": "K8siBackupRun",
        "metadata": {
            "name": run_name,
            "namespace": namespace,
            "labels": {f"{GROUP}/backup": name},
            "ownerReferences": [
                {
                    "apiVersion": f"{GROUP}/v1",
                    "kind": "K8siBackup",
                    "name": name,
                    "uid": backup_obj["metadata"]["uid"],
                    "controller": True,
                    "blockOwnerDeletion": False,
                }
            ],
        },
        "spec": {
            "backupRef": name,
            "triggeredBy": "manual",
            "triggeredAt": triggered_at,
            "mode": mode,
        },
    }

    try:
        custom.create_namespaced_custom_object(GROUP, VERSION, namespace, RUN_PLURAL, run_obj)
    except kubernetes.client.exceptions.ApiException as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Best-effort: immediately mark parent as running so counter tiles reflect active state.
    try:
        custom.patch_namespaced_custom_object_status(
            GROUP,
            VERSION,
            namespace,
            PLURAL,
            name,
            {"status": {"lastBackupResult": "running", "lastRunRef": run_name}},
        )
    except Exception:
        pass  # Non-fatal — run was created; counter will catch up on next poll.

    return {"triggered": True, "triggeredAt": triggered_at, "runName": run_name}


@app.patch("/api/backups/{namespace}/{name}/paused")
def set_paused(namespace: str, name: str, body: dict[str, Any]) -> dict[str, Any]:
    custom = kubernetes.client.CustomObjectsApi()
    try:
        custom.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"{namespace}/{name} not found")
        raise HTTPException(status_code=500, detail=str(e))

    paused = bool(body.get("paused", False))
    try:
        custom.patch_namespaced_custom_object(
            GROUP, VERSION, namespace, PLURAL, name, {"spec": {"paused": paused}}
        )
    except kubernetes.client.exceptions.ApiException as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"paused": paused}


@app.get("/api/runs/{namespace}/{run_name}/logs")
async def stream_run_logs(namespace: str, run_name: str) -> StreamingResponse:
    """SSE stream for a specific K8siBackupRun — polls phase and log until terminal."""
    custom = kubernetes.client.CustomObjectsApi()
    try:
        custom.get_namespaced_custom_object(GROUP, VERSION, namespace, RUN_PLURAL, run_name)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"run {namespace}/{run_name} not found")
        raise HTTPException(status_code=500, detail=str(e))

    async def _generate():
        seen = 0
        consecutive_errors = 0
        yield ": connected\n\n"
        while True:  # poll until terminal phase or client disconnect
            try:
                obj = await asyncio.to_thread(
                    custom.get_namespaced_custom_object,
                    GROUP,
                    VERSION,
                    namespace,
                    RUN_PLURAL,
                    run_name,
                )
                consecutive_errors = 0
            except kubernetes.client.exceptions.ApiException as e:
                if e.status == 404:
                    yield f"data: {json.dumps({'type': 'error', 'message': 'run not found'})}\n\n"
                    return
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    msg = f"API unavailable after {consecutive_errors} attempts"
                    yield f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"
                    return
                await asyncio.sleep(2)
                continue
            except Exception:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    msg = f"API unavailable after {consecutive_errors} attempts"
                    yield f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"
                    return
                await asyncio.sleep(2)
                continue

            status = obj.get("status", {})
            run_log = status.get("log", [])
            phase = status.get("phase", "Pending")

            for entry in run_log[seen:]:
                yield f"data: {json.dumps({'type': 'phase', **entry})}\n\n"
            seen = len(run_log)

            if phase in ("Succeeded", "Failed"):
                result = "success" if phase == "Succeeded" else "failed"
                done_payload = {
                    "type": "done",
                    "result": result,
                    "phase": phase,
                    "startTime": status.get("startTime"),
                    "completionTime": status.get("completionTime"),
                    "message": status.get("message", ""),
                }
                yield f"data: {json.dumps(done_payload)}\n\n"
                return

            await asyncio.sleep(2)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/backups/{namespace}/{name}/logs")
async def stream_logs(
    namespace: str, name: str, since: str | None = Query(None)
) -> StreamingResponse:
    """Legacy SSE endpoint — watches K8siBackup.status.lastRunLog."""
    custom = kubernetes.client.CustomObjectsApi()
    try:
        custom.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"{namespace}/{name} not found")
        raise HTTPException(status_code=500, detail=str(e))

    async def _generate():
        seen = 0
        log_lines_seen = 0
        since_dt = _parse_since(since)
        yield ": connected\n\n"
        for _ in range(300):
            try:
                obj = await asyncio.to_thread(
                    custom.get_namespaced_custom_object,
                    GROUP,
                    VERSION,
                    namespace,
                    PLURAL,
                    name,
                )
            except Exception:
                await asyncio.sleep(2)
                continue

            status = obj.get("status", {})
            run_log = status.get("lastRunLog", [])

            if len(run_log) < seen:
                seen = 0

            for entry in run_log[seen:]:
                if not _is_stale_entry(entry, since_dt):
                    yield f"data: {json.dumps({'type': 'phase', **entry})}\n\n"
            seen = len(run_log)

            result = status.get("lastBackupResult")
            last_time = status.get("lastBackupTime")
            if result == "running":
                try:
                    v1 = kubernetes.client.CoreV1Api()
                    pods = await asyncio.to_thread(v1.list_namespaced_pod, namespace)
                    for pod in pods.items:
                        labels = pod.metadata.labels or {}
                        if labels.get("job-name", "").startswith(f"k8si-{name}-"):
                            logs = await asyncio.to_thread(
                                v1.read_namespaced_pod_log,
                                pod.metadata.name,
                                namespace,
                            )
                            all_lines = (logs or "").splitlines()
                            for line in all_lines[log_lines_seen:]:
                                if line.strip():
                                    yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"
                            log_lines_seen = len(all_lines)
                            break
                except Exception:
                    pass
            elif result in ("success", "failed") and seen > 0 and _is_new_run(last_time, since):
                yield f"data: {json.dumps({'type': 'done', 'result': result})}\n\n"
                return

            await asyncio.sleep(2)

        yield f"data: {json.dumps({'type': 'done', 'result': 'timeout'})}\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/version")
def get_version() -> dict[str, str]:
    # K8SI_VERSION is injected at image build time; takes priority over package metadata.
    # The UI container copies app.py directly (no pip install), so importlib.metadata
    # returns PackageNotFoundError there unless this env var is set.
    ver = os.environ.get("K8SI_VERSION")
    if not ver:
        try:
            ver = importlib.metadata.version("k8si")
        except importlib.metadata.PackageNotFoundError:
            ver = "dev"
    return {"version": ver}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    here = os.path.dirname(__file__)
    return FileResponse(os.path.join(here, "dashboard.html"))

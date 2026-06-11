"""k8si web UI — read-only backup status dashboard."""

import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import kubernetes
import kubernetes.client
import kubernetes.client.exceptions
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

GROUP = "k8si.io"
VERSION = "v1"
PLURAL = "k8sibackups"


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
    """Compute successRate and streak from a recentBackups list (most-recent-first)."""
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
        "message": status.get("message", ""),
        "recentBackups": recent,
        "successRate": stats["successRate"],
        "streak": stats["streak"],
        "lastRestoreResult": status.get("lastRestoreResult"),
        "lastRestoreTime": status.get("lastRestoreTime"),
        "lastRestoreMessage": status.get("lastRestoreMessage"),
    }


@app.post("/api/backups/{namespace}/{name}/trigger")
def trigger_backup(namespace: str, name: str) -> dict[str, Any]:
    custom = kubernetes.client.CustomObjectsApi()
    try:
        custom.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            raise HTTPException(status_code=404, detail=f"{namespace}/{name} not found")
        raise HTTPException(status_code=500, detail=str(e))

    now = datetime.now(tz=UTC).isoformat()
    try:
        custom.patch_namespaced_custom_object_status(
            GROUP,
            VERSION,
            namespace,
            PLURAL,
            name,
            {"status": {"triggeredAt": now}},
        )
    except kubernetes.client.exceptions.ApiException as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"triggered": True, "triggeredAt": now}


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
            GROUP,
            VERSION,
            namespace,
            PLURAL,
            name,
            {"spec": {"paused": paused}},
        )
    except kubernetes.client.exceptions.ApiException as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"paused": paused}


@app.get("/")
def index() -> FileResponse:
    here = os.path.dirname(__file__)
    return FileResponse(os.path.join(here, "dashboard.html"))

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


def _shape(item: dict[str, Any]) -> dict[str, Any]:
    meta = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})
    return {
        "name": meta.get("name", ""),
        "namespace": meta.get("namespace", ""),
        "pvc": spec.get("pvc", ""),
        "schedule": spec.get("schedule", ""),
        "paused": spec.get("paused", False),
        "lastBackupTime": status.get("lastBackupTime"),
        "lastBackupResult": status.get("lastBackupResult", "pending"),
        "nextBackupTime": status.get("nextBackupTime"),
        "triggeredAt": status.get("triggeredAt"),
        "message": status.get("message", ""),
        "recentBackups": status.get("recentBackups", []),
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
            GROUP, VERSION, namespace, PLURAL, name,
            {"status": {"triggeredAt": now}},
        )
    except kubernetes.client.exceptions.ApiException as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"triggered": True, "triggeredAt": now}


@app.get("/")
def index() -> FileResponse:
    here = os.path.dirname(__file__)
    return FileResponse(os.path.join(here, "dashboard.html"))

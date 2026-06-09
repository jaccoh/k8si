"""k8si web UI — read-only backup status dashboard."""

import os
from typing import Any

import kubernetes
import kubernetes.client
from fastapi import FastAPI
from fastapi.responses import FileResponse

app = FastAPI()

GROUP = "k8si.io"
VERSION = "v1"
PLURAL = "k8sibackups"


def _load_k8s() -> None:
    try:
        kubernetes.config.load_incluster_config()
    except kubernetes.config.ConfigException:
        kubernetes.config.load_kube_config()


@app.on_event("startup")
def startup() -> None:
    _load_k8s()


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
        "lastBackupTime": status.get("lastBackupTime"),
        "lastBackupResult": status.get("lastBackupResult", "pending"),
        "nextBackupTime": status.get("nextBackupTime"),
        "message": status.get("message", ""),
        "recentBackups": status.get("recentBackups", []),
    }


@app.get("/")
def index() -> FileResponse:
    here = os.path.dirname(__file__)
    return FileResponse(os.path.join(here, "dashboard.html"))

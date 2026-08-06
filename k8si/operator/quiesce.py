"""Database quiescing before VolumeSnapshot — purely Pythonic, no kubectl."""

import asyncio
import base64
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import kubernetes
import kubernetes.client

log = logging.getLogger(__name__)


def _read_secret_sync(namespace: str, secret_name: str) -> dict[str, str]:
    v1 = kubernetes.client.CoreV1Api()
    secret = v1.read_namespaced_secret(secret_name, namespace)
    return {k: base64.b64decode(v).decode() for k, v in (secret.data or {}).items()}


def _expand_db_host(creds: dict[str, str], namespace: str) -> dict[str, str]:
    """Expand a short DB_HOST to FQDN so the operator (in k8si-system) can resolve it."""
    host = creds.get("DB_HOST", "")
    if host and "." not in host:
        creds = dict(creds)
        creds["DB_HOST"] = f"{host}.{namespace}.svc.cluster.local"
        log.info("Expanded DB_HOST to FQDN: %s", creds["DB_HOST"])
    return creds


def _find_pod_name_sync(namespace: str, selector: dict[str, str]) -> str:
    v1 = kubernetes.client.CoreV1Api()
    label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
    pods = v1.list_namespaced_pod(namespace, label_selector=label_selector)
    running = [p for p in pods.items if p.status and p.status.phase == "Running"]
    if not running:
        raise RuntimeError(f"No running pod with selector {selector!r} in {namespace}")
    return running[0].metadata.name  # type: ignore[no-any-return]


def _exec_sync_in_pod_sync(namespace: str, selector: dict[str, str]) -> None:
    try:
        pod_name = _find_pod_name_sync(namespace, selector)
        from kubernetes.stream import stream

        v1 = kubernetes.client.CoreV1Api()
        stream(
            v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=["sync"],
            stderr=False,
            stdin=False,
            stdout=False,
            tty=False,
        )
        log.info("Exec sync in pod %s/%s succeeded", namespace, pod_name)
    except Exception as e:
        log.debug("Exec sync in pod failed (continuing): %s", e)


def _sqlite_checkpoint_sync(namespace: str, pod_name: str, db_paths: list[str]) -> None:
    from kubernetes.stream import stream

    v1 = kubernetes.client.CoreV1Api()
    for db_path in db_paths:
        candidates = [
            [
                "python3",
                "-c",
                (
                    f"import sqlite3, os; c=sqlite3.connect({db_path!r}); "
                    f"c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close(); "
                    f"os.sync(); print('checkpointed')"
                ),
            ],
            ["sh", "-c", f"sqlite3 {db_path} 'PRAGMA wal_checkpoint(TRUNCATE);' && sync"],
        ]
        checkpointed = False
        for cmd in candidates:
            try:
                resp = stream(
                    v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    namespace,
                    command=cmd,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                )
                log.info(
                    "SQLite checkpoint %s in %s/%s via %s: %s",
                    db_path,
                    namespace,
                    pod_name,
                    cmd[0],
                    (resp or "").strip(),
                )
                checkpointed = True
                break
            except Exception as e:
                log.debug("SQLite checkpoint via %s failed: %s", cmd[0], e)
        if not checkpointed:
            log.warning(
                "SQLite checkpoint skipped for %s in %s/%s: no python3 or sqlite3 in container",
                db_path,
                namespace,
                pod_name,
            )


def _mariadb_ftwrl_sync(creds: dict[str, str]) -> Any:
    import pymysql

    conn = pymysql.connect(
        host=creds.get("DB_HOST", "localhost"),
        port=int(creds.get("DB_PORT", "3306")),
        user=creds["DB_USER"],
        password=creds["DB_PASSWORD"],
        database=creds.get("DB_NAME", ""),
    )
    with conn.cursor() as cur:
        cur.execute("FLUSH TABLES WITH READ LOCK")
    log.info("MariaDB: FLUSH TABLES WITH READ LOCK acquired")
    return conn


def _mariadb_unlock_sync(conn: Any) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("UNLOCK TABLES")
        log.info("MariaDB: UNLOCK TABLES")
    finally:
        conn.close()


def _postgres_checkpoint_sync(creds: dict[str, str]) -> None:
    import psycopg

    conn = psycopg.connect(
        host=creds.get("DB_HOST", "localhost"),
        port=int(creds.get("DB_PORT", "5432")),
        user=creds["DB_USER"],
        password=creds["DB_PASSWORD"],
        dbname=creds.get("DB_NAME", "postgres"),
        autocommit=True,
    )
    with conn.cursor() as cur:
        cur.execute("CHECKPOINT")
    conn.close()
    log.info("Postgres: CHECKPOINT complete")


@asynccontextmanager
async def quiesce_context(
    db_spec: dict[str, Any] | None,
    namespace: str,
    logger: logging.Logger,
):
    """Context manager: quiesces the DB before the snapshot, releases after."""
    if db_spec is None:
        yield
        return

    db_type = db_spec["type"]

    if db_type == "mariadb":
        creds = await asyncio.to_thread(_read_secret_sync, namespace, db_spec["secretRef"])
        creds = _expand_db_host(creds, namespace)
        conn = await asyncio.to_thread(_mariadb_ftwrl_sync, creds)
        log.warning(
            "MariaDB: write lock acquired (FTWRL) — held until snapshot completes, max ~300s"
        )
        if "podSelector" in db_spec:
            await asyncio.to_thread(_exec_sync_in_pod_sync, namespace, db_spec["podSelector"])
        _lock_start = time.monotonic()
        try:
            yield
        finally:
            _elapsed = time.monotonic() - _lock_start
            log.info("MariaDB: releasing write lock after %.1fs", _elapsed)
            await asyncio.to_thread(_mariadb_unlock_sync, conn)

    elif db_type == "postgres":
        creds = await asyncio.to_thread(_read_secret_sync, namespace, db_spec["secretRef"])
        creds = _expand_db_host(creds, namespace)
        await asyncio.to_thread(_postgres_checkpoint_sync, creds)
        if "podSelector" in db_spec:
            await asyncio.to_thread(_exec_sync_in_pod_sync, namespace, db_spec["podSelector"])
        logger.info("Postgres checkpointed; taking snapshot")
        yield

    elif db_type == "sqlite":
        selector = db_spec.get("podSelector", {})
        db_paths = db_spec.get("dbPaths", [])
        if selector and db_paths:
            pod_name = await asyncio.to_thread(_find_pod_name_sync, namespace, selector)
            await asyncio.to_thread(_sqlite_checkpoint_sync, namespace, pod_name, db_paths)
            await asyncio.to_thread(_exec_sync_in_pod_sync, namespace, selector)
        else:
            logger.warning("database.type=sqlite: need podSelector + dbPaths; skipping checkpoint")
        yield

    else:
        raise ValueError(f"Unknown database.type: {db_type!r}")

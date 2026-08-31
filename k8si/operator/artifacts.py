"""Artifact parsing — snapshot ID and size from backup job output.

Primary source is the structured K8SI_ARTIFACT JSON line the backup job emits
(resolved from the backend's own metadata API, see k8si/backup.py); the
human-readable log regexes are the fallback for jobs that predate it or fail
to resolve.
"""

import json
import re

# Structured artifact line emitted by the backup job (see k8si/backup.py) —
# the reliable artifact source; the log regexes below are the fallback.
ARTIFACT_MARKER = "K8SI_ARTIFACT "

_SIZE_UNITS: dict[str, int] = {
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
}


def _parse_structured_artifact(logs: str) -> tuple[str | None, int | None]:
    """Parse the last K8SI_ARTIFACT JSON line the backup job emitted.

    The job resolves the artifact from the backend's own metadata API
    (restic snapshots --json / kopia snapshot create --json), so this line is
    authoritative when present. Returns (None, None) when absent or invalid —
    the caller then falls back to scraping the human-readable logs.
    """
    # Walk ALL occurrences, last-first: if the job ever emits the marker more
    # than once, a trailing broken copy must not invalidate an earlier valid
    # one (rfind + single parse died on exactly that in production).
    start = len(logs)
    while True:
        idx = logs.rfind(ARTIFACT_MARKER, 0, start)
        if idx == -1:
            return None, None
        start = idx
        tail = logs[idx + len(ARTIFACT_MARKER) :]
        line = tail.splitlines()[0] if tail.splitlines() else ""
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("snapshotId"):
            size = payload.get("sizeBytes")
            return str(payload["snapshotId"]), size if isinstance(size, int) else None


def _parse_artifact(logs: str, backend_type: str) -> tuple[str | None, int | None]:
    """Parse snapshot ID and total size in bytes from backup job stdout.

    Returns (snapshot_id, size_bytes). Either field may be None if not found.
    """
    snap_id, size_bytes = _parse_structured_artifact(logs)
    if snap_id:
        return snap_id, size_bytes

    if backend_type == "restic":
        snap_id = None
        m = re.search(r"snapshot ([a-f0-9]+) saved", logs)
        if m:
            snap_id = m.group(1)

        size_bytes = None
        # "processed N files, 23.456 GiB in 0:00"
        m2 = re.search(
            r"processed \d+ files?,\s+([\d.]+)\s*([A-Za-z]+)\s+in\b",
            logs,
        )
        if m2:
            amount, unit = float(m2.group(1)), m2.group(2).lower()
            multiplier = _SIZE_UNITS.get(unit)
            if multiplier is not None:
                size_bytes = int(amount * multiplier)

        return snap_id, size_bytes

    if backend_type == "kopia":
        snap_id = None
        # Modern kopia: "Created snapshot with root <root> and ID <id> in Ns."
        m = re.search(r"Created snapshot with root \S+ and ID (\S+)", logs)
        if m:
            snap_id = m.group(1).rstrip(".")
        else:
            # Legacy kopia: "Snapshotted source and ID <id> in 0:05"
            m = re.search(r"Snapshotted source and ID (\S+)", logs)
            if m:
                snap_id = m.group(1)

        size_bytes = None
        # Progress lines: "* N hashing, N hashed (SIZE), N cached (SIZE), N uploading".
        # A first run reports data under "hashed" with "cached (0 B)" last, so take
        # the max across both rather than the last "cached" match.
        for m2 in re.finditer(r"(?:hash|cach)ed \(([\d.]+)\s*([A-Za-z]+)\)", logs):
            amount = float(m2.group(1))
            unit = m2.group(2).lower()
            multiplier = _SIZE_UNITS.get(unit)
            if multiplier is not None:
                candidate = int(amount * multiplier)
                if size_bytes is None or candidate > size_bytes:
                    size_bytes = candidate

        return snap_id, size_bytes

    return None, None

"""Unit tests for restic/kopia job-log artifact parsing (_parse_artifact)."""

from k8si.operator.workflow import _parse_artifact

# ---------------------------------------------------------------------------
# Restic
# ---------------------------------------------------------------------------

RESTIC_SUCCESS_LOGS = """\
open repository
lock repository
no parent snapshot found, will read all files
scan [/data]

Files:           4 new,     0 changed,     0 unmodified
Dirs:            2 new,     0 changed,     0 unmodified
Added to the repository: 1.234 KiB (857 B stored)

processed 4 files, 1.234 KiB in 0:00
snapshot abc12345 saved
"""

RESTIC_GIB_LOGS = """\
Files:        1234 new,   567 changed,     0 unmodified
Added to the repository: 23.456 GiB (compressed: 18.123 GiB)
processed 1234 files, 23.456 GiB in 14:32
snapshot deadbeef saved
"""

RESTIC_MIB_LOGS = """\
Files:          50 new,     0 changed,     0 unmodified
Added to the repository: 512.000 MiB (384 MiB stored)
processed 50 files, 512.000 MiB in 2:15
snapshot cafef00d saved
"""

RESTIC_NO_SNAPSHOT = """\
Fatal: unable to open config file: Stat: The specified key does not exist.
Is there a repository at the following location?
"""


def test_parse_restic_snapshot_id():
    snap_id, _ = _parse_artifact(RESTIC_SUCCESS_LOGS, "restic")
    assert snap_id == "abc12345"


def test_parse_restic_snapshot_id_gib():
    snap_id, _ = _parse_artifact(RESTIC_GIB_LOGS, "restic")
    assert snap_id == "deadbeef"


def test_parse_restic_size_kib():
    _, size = _parse_artifact(RESTIC_SUCCESS_LOGS, "restic")
    # 1.234 KiB = 1.234 * 1024 = ~1263 bytes
    assert size is not None
    assert 1200 < size < 1300


def test_parse_restic_size_gib():
    _, size = _parse_artifact(RESTIC_GIB_LOGS, "restic")
    # 23.456 GiB
    assert size is not None
    expected = int(23.456 * 1024**3)
    assert abs(size - expected) < 1024**2  # within 1 MiB tolerance


def test_parse_restic_size_mib():
    _, size = _parse_artifact(RESTIC_MIB_LOGS, "restic")
    assert size is not None
    expected = int(512.0 * 1024**2)
    assert abs(size - expected) < 1024


def test_parse_restic_no_snapshot_returns_none():
    snap_id, size = _parse_artifact(RESTIC_NO_SNAPSHOT, "restic")
    assert snap_id is None
    assert size is None


def test_parse_empty_logs_returns_none():
    snap_id, size = _parse_artifact("", "restic")
    assert snap_id is None
    assert size is None


# ---------------------------------------------------------------------------
# Kopia
# ---------------------------------------------------------------------------

# Modern kopia: "Created snapshot with root <root> and ID <id> in Ns."
KOPIA_MODERN_LOGS = """\
Snapshotting /data ...
  * 0 hashing, 0 cached (0 B), 0 uploading
  * 1234 hashing, 5678 cached (23.456 GiB), 12 uploading
  * 0 hashing, 6912 cached (25.1 GiB), 0 uploading, done (14s)
 Uploaded 1.644 GiB.
Created snapshot with root kabcde1234567890aabbccddeeff0011 and ID kdeadbeef0099aabb in 14s.
"""

# Older kopia: "Snapshotted source and ID <id> in 0:05"
KOPIA_LEGACY_LOGS = """\
Snapshotting source ...
  * 0 hashing, 42 cached (1.234 GiB), 0 uploading
  Snapshotted source and ID kabcdef1234567890 in 0:05
"""

# Kopia with no snapshot (error path)
KOPIA_ERROR_LOGS = """\
ERROR: unable to connect to repository
"""


def test_parse_kopia_modern_snapshot_id():
    """Modern kopia output: 'Created snapshot with root ... and ID <id>'."""
    snap_id, _ = _parse_artifact(KOPIA_MODERN_LOGS, "kopia")
    assert snap_id == "kdeadbeef0099aabb"


def test_parse_kopia_legacy_snapshot_id():
    """Older kopia output: 'Snapshotted source and ID <id>'."""
    snap_id, _ = _parse_artifact(KOPIA_LEGACY_LOGS, "kopia")
    assert snap_id == "kabcdef1234567890"


def test_parse_kopia_modern_size():
    """Modern kopia: parse size from 'cached (SIZE)' progress line (last occurrence)."""
    _, size = _parse_artifact(KOPIA_MODERN_LOGS, "kopia")
    assert size is not None
    expected = int(25.1 * 1024**3)
    assert abs(size - expected) < 1024**2  # within 1 MiB tolerance


def test_parse_kopia_legacy_size():
    """Legacy kopia: parse size from 'cached (SIZE)' progress line."""
    _, size = _parse_artifact(KOPIA_LEGACY_LOGS, "kopia")
    assert size is not None
    expected = int(1.234 * 1024**3)
    assert abs(size - expected) < 1024**2


def test_parse_kopia_error_returns_none():
    """Kopia error output: no snapshot ID or size."""
    snap_id, size = _parse_artifact(KOPIA_ERROR_LOGS, "kopia")
    assert snap_id is None
    assert size is None


# Real kopia 0.15.0 first-run output (captured from the k8si image): everything
# is hashed, nothing is cached, so the last "cached (N B)" line says 0 B — the
# size must come from the max of hashed/cached, not the last cached match.
KOPIA_FIRST_RUN_LOGS = (
    "Snapshotting root@51bd9fa6def5:/data ...\n"
    " * 0 hashing, 0 hashed (0 B), 0 cached (0 B), uploaded 0 B, estimating...\n"
    " * 0 hashing, 2 hashed (14 B), 0 cached (0 B), uploaded 195 B, estimating...\n"
    # assembled to one logical line: "Created snapshot with root <id> and ID <id> in 0s"
    "Created snapshot with root ked2772c42cdaf458bedc3aa8ef5b5e6d"
    " and ID 5fc52d496b7a5c7866fd6ca1f9d8d2c2 in 0s\n"
)


def test_parse_kopia_first_run_size_from_hashed():
    """First-run kopia reports data under 'hashed (N B)' with 'cached (0 B)' —
    size must be the max across hashed/cached, not the last cached value (0)."""
    snap_id, size = _parse_artifact(KOPIA_FIRST_RUN_LOGS, "kopia")
    assert snap_id == "5fc52d496b7a5c7866fd6ca1f9d8d2c2"
    assert size == 14


# ---------------------------------------------------------------------------
# Structured artifact line (K8SI_ARTIFACT) — the reliable path
# ---------------------------------------------------------------------------

RESTIC_STRUCTURED_LOGS = """\
open repository
lock repository
processed 4 files, 1.234 KiB in 0:00
snapshot abc12345 saved
K8SI_ARTIFACT {"snapshotId": "deadbeef99", "sizeBytes": 2048}
"""

KOPIA_STRUCTURED_LOGS = """\
Snapshotting /data ...
  * 1234 hashing, 5678 cached (23.456 GiB), 12 uploading
Created snapshot with root kabcde1234567890aabbccddeeff0011 and ID kregexid0099aabb in 14s.
K8SI_ARTIFACT {"snapshotId": "kstructured77", "sizeBytes": 3072}
"""


def test_parse_restic_prefers_structured_over_regex():
    """The structured line comes from the backend's own metadata API (restic
    snapshots --json / kopia snapshot create --json) and must win over the
    regex-scraped values when both are present."""
    snap_id, size = _parse_artifact(RESTIC_STRUCTURED_LOGS, "restic")
    assert snap_id == "deadbeef99"
    assert size == 2048


def test_parse_kopia_prefers_structured_over_regex():
    snap_id, size = _parse_artifact(KOPIA_STRUCTURED_LOGS, "kopia")
    assert snap_id == "kstructured77"
    assert size == 3072


def test_parse_structured_marker_without_size():
    logs = 'K8SI_ARTIFACT {"snapshotId": "abc"}\n'
    snap_id, size = _parse_artifact(logs, "restic")
    assert snap_id == "abc"
    assert size is None


def test_parse_structured_marker_malformed_json_falls_back_to_regex():
    logs = (
        "snapshot abc12345 saved\n"
        "processed 4 files, 1.234 KiB in 0:00\n"
        'K8SI_ARTIFACT {"snapshotId": broken-json\n'
    )
    snap_id, size = _parse_artifact(logs, "restic")
    assert snap_id == "abc12345"
    assert size is not None and 1200 < size < 1300


def test_parse_structured_marker_without_snapshot_id_ignored():
    """A payload lacking snapshotId carries no artifact — ignore it."""
    logs = 'snapshot abc12345 saved\nK8SI_ARTIFACT {"sizeBytes": 42}\n'
    snap_id, _ = _parse_artifact(logs, "restic")
    assert snap_id == "abc12345"


def test_parse_structured_marker_non_dict_payload_ignored():
    logs = "snapshot abc12345 saved\nK8SI_ARTIFACT [1, 2, 3]\n"
    snap_id, _ = _parse_artifact(logs, "restic")
    assert snap_id == "abc12345"


def test_parse_takes_last_structured_marker():
    """If a job ever emits two markers, the last one (from the final retry) wins."""
    logs = 'K8SI_ARTIFACT {"snapshotId": "first"}\nK8SI_ARTIFACT {"snapshotId": "second"}\n'
    snap_id, _ = _parse_artifact(logs, "restic")
    assert snap_id == "second"


# ── live regression: kopia progress uses \r, marker must survive ──────────────

KOPIA_LIVE_BLOB = (
    "2026-08-31 INFO k8si.backup PVC backup job starting. Repo: sftp://x\n"
    "2026-08-31 INFO k8si.backends.kopia kopia: Connected to repository.\n"
    " * 0 hashing, 351 hashed (199.7 MB), 0 cached (0 B), uploaded 216 B, "
    "estimated 199.7 MB (100.0%) 0s left   \r"
    '{"id":"e27632fe0a3f4ce60c25bb330d266a5a","source":{"host":"k8si-backup",'
    '"userName":"root","path":"/data"},"rootEntry":{"summ":{"size":199688393}}}\n'
    'K8SI_ARTIFACT {"snapshotId": "e27632fe0a3f4ce60c25bb330d266a5a", '
    '"sizeBytes": 199688393}\n'
    "2026-08-31 INFO k8si.backends.kopia kopia: Setting policy for root@k8si-backup:/data\n"
    "2026-08-31 INFO k8si.backup PVC backup complete.\n"
)

KOPIA_LIVE_BLOB_NO_NEWLINE_BEFORE_MANIFEST = KOPIA_LIVE_BLOB.replace(
    "0s left   \r", "0s left   "
).replace(
    '"rootEntry":{"summ":{"size":199688393}}}\n',
    '\r{"id":"e27632fe0a3f4ce60c25bb330d266a5a","source":{"host":"k8si-backup",'
    '"userName":"root","path":"/data"},"rootEntry":{"summ":{"size":199688393}}}\n',
    1,
)


def test_structured_artifact_survives_kopia_carriage_progress() -> None:
    """Live regression (2026-08-31, sonarr/radarr kopia migration): the
    operator logged 'Could not parse snapshot ID' on real kopia jobs whose
    logs DO contain the K8SI_ARTIFACT line — the \r-laced progress stream is
    the suspect. The marker parse must hold against realistic blobs."""
    from k8si.operator.artifacts import _parse_artifact

    for name, blob in (
        ("clean", KOPIA_LIVE_BLOB),
        ("cr-mixed", KOPIA_LIVE_BLOB_NO_NEWLINE_BEFORE_MANIFEST),
    ):
        snap, size = _parse_artifact(blob, "kopia")
        assert snap == "e27632fe0a3f4ce60c25bb330d266a5a", f"{name}: marker parse failed"
        assert size == 199688393, f"{name}: exact size lost"

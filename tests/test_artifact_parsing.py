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

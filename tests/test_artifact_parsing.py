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
# Kopia (stub — returns None until kopia parsing is implemented)
# ---------------------------------------------------------------------------

KOPIA_LOGS = """\
Snapshotting source ...
  * 0 hashing, 42 cached (1.2 GB), 0 uploading
  Snapshotted source and ID kabcdef1234567890 in 0:05
"""


def test_parse_kopia_returns_none_stub():
    """Kopia parsing is not implemented yet — expect None, None."""
    snap_id, size = _parse_artifact(KOPIA_LOGS, "kopia")
    assert snap_id is None
    assert size is None

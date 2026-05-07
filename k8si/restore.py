"""Init container mode: restore from restic if sentinel is missing."""

import logging

from .config import Config
from .restic import Restic, ResticError, ResticNoSnapshotsError

log = logging.getLogger(__name__)


def run(config: Config, restic: Restic) -> None:
    assert config.sentinel_file is not None
    sentinel = config.data_path / config.sentinel_file

    if sentinel.exists():
        log.info("Sentinel present, skipping restore: %s", sentinel)
        return

    log.info("Sentinel missing, restoring from %s", config.restic_repository)

    try:
        restic.restore()
        log.info("Restore complete. App will write sentinel on first init.")
    except ResticNoSnapshotsError:
        log.info("No snapshots found — first deploy, starting fresh.")
    except ResticError as e:
        log.error("Restore failed: %s", e.stderr)
        raise SystemExit(1) from e

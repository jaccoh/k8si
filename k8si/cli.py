"""Entrypoint: 'k8si generate' for YAML generation, 'k8si' for runtime (MODE env var)."""

import argparse
import logging
import os
import sys

from .config import Config, ConfigError
from .restic import Restic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def main() -> None:
    # 'k8si generate ...' → offline YAML generator, no env vars needed
    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        from . import generate

        parser = argparse.ArgumentParser(prog="k8si")
        subparsers = parser.add_subparsers()
        generate.add_parser(subparsers)
        args = parser.parse_args()
        args.func(args)
        return

    # 'k8si' → runtime mode, reads MODE env var
    try:
        config = Config.from_env()
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)

    restic = Restic(env=_build_restic_env(config))

    if config.mode == "restore":
        from . import restore
        restore.run(config, restic)
    else:
        from . import backup
        backup.run(config, restic)


def _build_restic_env(config: Config) -> dict[str, str]:
    env = dict(os.environ)
    env["RESTIC_REPOSITORY"] = config.restic_repository

    if config.restic_password:
        env["RESTIC_PASSWORD"] = config.restic_password
    elif config.restic_password_file:
        env["RESTIC_PASSWORD_FILE"] = str(config.restic_password_file)

    return env

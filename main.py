from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import ConfigError
from scheduler import Scheduler


def _configure_logging(log_path: Path) -> logging.Logger:
    logger = logging.getLogger("radioarchive")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    fh = RotatingFileHandler(
        filename=str(log_path),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)

    logger.addHandler(sh)
    logger.addHandler(fh)
    return logger


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    log_path = app_dir / "radioarchive.log"
    logger = _configure_logging(log_path)

    config_path = app_dir / "schedule.yaml"
    scheduler = Scheduler(config_path=config_path, logger=logger)

    try:
        scheduler.load_initial_config()
    except ConfigError as e:
        logger.error("Config error: %s", e)
        return 2
    except Exception as e:  # noqa: BLE001
        logger.exception("Startup failed: %s", e)
        return 1

    try:
        scheduler.run_forever()
    except KeyboardInterrupt:
        logger.info("Shutdown requested (Ctrl+C). Stopping active recordings...")
        scheduler.shutdown()
        logger.info("Shutdown complete.")
        return 0
    except Exception as e:  # noqa: BLE001
        logger.exception("Fatal error: %s", e)
        try:
            scheduler.shutdown()
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

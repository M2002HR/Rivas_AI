from __future__ import annotations

import time
import sys

from .app import RivasApp
from .config import BotConfig, ConfigError
from .logging_utils import get_logger, setup_logging


def main() -> None:
    try:
        config = BotConfig.from_env()
    except ConfigError as exc:
        print(f"[config-error] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    setup_logging(config.log_level)
    log = get_logger("rivas.main")
    log.info("Starting Rivas bot in %s", config.app_env)

    retry_seconds = 3
    while True:
        try:
            app = RivasApp(config)
            app.run()
            return
        except KeyboardInterrupt:
            return
        except Exception as exc:
            log.exception("bot_runtime_crashed | retry_in_seconds=%s | error=%s", retry_seconds, exc)
            time.sleep(retry_seconds)
            retry_seconds = min(30, retry_seconds * 2)


if __name__ == "__main__":
    main()

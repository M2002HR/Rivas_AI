from __future__ import annotations

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

    app = RivasApp(config)
    app.run()


if __name__ == "__main__":
    main()

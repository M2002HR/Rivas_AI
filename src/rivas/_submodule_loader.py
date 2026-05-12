from __future__ import annotations

import sys
from pathlib import Path


def ensure_mira_submodule_importable() -> None:
    try:
        import mira_telegram_service  # noqa: F401
        return
    except Exception:
        pass

    root = Path(__file__).resolve().parents[2]
    candidate = root / "submodules" / "mira-telegram-service" / "src"
    if candidate.exists():
        path_str = str(candidate)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)

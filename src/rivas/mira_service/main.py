from .._submodule_loader import ensure_mira_submodule_importable

ensure_mira_submodule_importable()

from mira_telegram_service.main import main

__all__ = ["main"]

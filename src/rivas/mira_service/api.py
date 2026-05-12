from .._submodule_loader import ensure_mira_submodule_importable

ensure_mira_submodule_importable()

from mira_telegram_service.api import MiraServiceAPI

__all__ = ["MiraServiceAPI"]

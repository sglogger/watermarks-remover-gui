"""Environment-driven settings.

Everything has a safe default, so the app starts with an empty `.env`. Defaults
are the conservative choice: loopback bind, no login, modest upload cap.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- Upstream engine -----------------------------------------------------
    core_url: str = field(
        default_factory=lambda: os.environ.get(
            "WR_CORE_URL", "http://127.0.0.1:8765"
        ).rstrip("/")
    )
    core_api_key: str = field(
        default_factory=lambda: os.environ.get("WATERMARKS_SERVER_API_KEY", "").strip()
    )
    core_timeout: float = field(default_factory=lambda: float(_int("WR_CORE_TIMEOUT", 120)))
    #: Mirrors the upstream `WATERMARKS_MAX_BATCH_FILES` cap so we chunk before
    #: the engine has to reject us. Overridden at runtime from /capabilities
    #: when the engine reports its own value.
    core_batch_cap: int = field(default_factory=lambda: _int("WR_CORE_BATCH_CAP", 50))

    # --- Our service ---------------------------------------------------------
    bind: str = field(default_factory=lambda: os.environ.get("GUI_BIND", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("GUI_PORT", 8080))
    auth_token: str = field(
        default_factory=lambda: os.environ.get("GUI_AUTH_TOKEN", "").strip()
    )
    rate_limit_per_min: int = field(
        default_factory=lambda: _int("GUI_RATE_LIMIT_PER_MIN", 0)
    )
    max_upload_mb: int = field(default_factory=lambda: _int("GUI_MAX_UPLOAD_MB", 32))
    max_files: int = field(default_factory=lambda: _int("GUI_MAX_FILES", 25))

    # --- Scan cache (memory only, never touches disk) ------------------------
    cache_ttl: int = field(default_factory=lambda: _int("GUI_CACHE_TTL", 600))
    cache_max_mb: int = field(default_factory=lambda: _int("GUI_CACHE_MAX_MB", 256))

    # --- Upstream update check (the app's only outbound call) ----------------
    update_check: bool = field(default_factory=lambda: _bool("GUI_UPDATE_CHECK", True))
    releases_url: str = field(
        default_factory=lambda: os.environ.get(
            "GUI_RELEASES_URL",
            "https://api.github.com/repos/guillaumemeyer/watermarks-remover/releases/latest",
        )
    )

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def cache_max_bytes(self) -> int:
        return self.cache_max_mb * 1024 * 1024

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_token)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide settings singleton (re-read with :func:`reset_settings`)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Drop the cached settings so the next call re-reads the environment."""
    global _settings
    _settings = None

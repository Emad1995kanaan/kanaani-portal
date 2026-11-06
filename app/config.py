"""Settings are read from environment variables (see .env.example). Never hard-code secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = field(default_factory=lambda: os.getenv("APP_NAME", "KANAANI"))
    environment: str = field(default_factory=lambda: os.getenv("ENVIRONMENT", "development"))
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./data/kanaani.db"))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", True))
    session_idle_minutes: int = field(default_factory=lambda: int(os.getenv("SESSION_IDLE_MINUTES", "30")))
    session_max_hours: int = field(default_factory=lambda: int(os.getenv("SESSION_MAX_HOURS", "12")))
    login_max_failures: int = field(default_factory=lambda: int(os.getenv("LOGIN_MAX_FAILURES", "5")))
    login_lock_minutes: int = field(default_factory=lambda: int(os.getenv("LOGIN_LOCK_MINUTES", "15")))
    rate_limit_per_minute: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_PER_MINUTE", "120")))
    enable_api_docs: bool = field(default_factory=lambda: _bool("ENABLE_API_DOCS", False))
    trusted_proxy: bool = field(default_factory=lambda: _bool("TRUSTED_PROXY", False))

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


def get_settings() -> Settings:
    s = Settings()
    if s.is_production and len(s.secret_key) < 32:
        raise RuntimeError("SECRET_KEY must be set (32+ random characters) in production.")
    return s


settings = get_settings()

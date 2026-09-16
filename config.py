"""Configuration for the TMW MCP server, loaded from the environment.

Values come from a `.env` file at the project root when present, falling back to
real environment variables. See `.env.example` for the full list of settings.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Raised when the environment is missing or malformed."""


def _require(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def _optional(name: str) -> str | None:
    return (os.getenv(name) or "").strip() or None


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


@dataclass(frozen=True)
class Settings:
    db_server: str
    db_database: str
    db_driver: str
    db_user: str | None
    db_password: str | None
    db_timeout: int
    checkcall_lookback_days: int


def load_settings() -> Settings:
    """Read settings from the environment, raising ConfigError if incomplete."""
    user = _optional("TMW_DB_USER")
    password = _optional("TMW_DB_PASSWORD")
    if bool(user) != bool(password):
        raise ConfigError(
            "TMW_DB_USER and TMW_DB_PASSWORD must be set together, or both left "
            "blank to use Windows Authentication."
        )

    return Settings(
        db_server=_require("TMW_DB_SERVER"),
        db_database=_require("TMW_DB_DATABASE"),
        db_driver=_require("TMW_DB_DRIVER"),
        db_user=user,
        db_password=password,
        db_timeout=_int("TMW_DB_TIMEOUT", 30),
        checkcall_lookback_days=_int("TMW_CHECKCALL_LOOKBACK_DAYS", 30),
    )

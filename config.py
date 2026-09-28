"""Configuration for the TMW MCP server, loaded from the environment.

Values come from a `.env` file at the project root when present, falling back to
real environment variables. See `.env.example` for the full list of settings.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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


def _csv(name: str) -> list[str]:
    raw = (os.getenv(name) or "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def _api_keys(name: str) -> dict[str, str]:
    """Parse comma-separated LABEL:SECRET pairs into {label: secret}.

    A bare secret with no label gets an auto-generated one. Labels exist so the
    access log can name which caller made a request, and so one key can be
    revoked without rotating the rest.
    """
    keys: dict[str, str] = {}
    for index, entry in enumerate(_csv(name), start=1):
        label, separator, secret = entry.partition(":")
        if not separator:
            label, secret = f"key{index}", entry
        label, secret = label.strip(), secret.strip()
        if not secret:
            raise ConfigError(f"{name} entry {index} has a label but no secret.")
        if label in keys:
            raise ConfigError(f"{name} has two entries labelled {label!r}.")
        keys[label] = secret
    return keys


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
    db_encrypt: str | None
    db_trust_server_certificate: str | None
    checkcall_lookback_days: int
    max_search_rows: int
    mcp_host: str
    mcp_port: int
    mcp_allowed_hosts: list[str]
    mcp_allowed_origins: list[str]
    api_keys: dict[str, str]


def load_settings() -> Settings:
    """Read settings from the environment, raising ConfigError if incomplete."""
    user = _optional("TMW_DB_USER")
    password = _optional("TMW_DB_PASSWORD")
    if bool(user) != bool(password):
        raise ConfigError(
            "TMW_DB_USER and TMW_DB_PASSWORD must be set together, or both left "
            "blank to use Windows Authentication."
        )

    mcp_host = _optional("TMW_MCP_HOST") or "127.0.0.1"
    api_keys = _api_keys("TMW_MCP_API_KEYS")
    if mcp_host not in LOOPBACK_HOSTS and not api_keys:
        raise ConfigError(
            f"TMW_MCP_HOST is {mcp_host!r}, which is reachable off this machine, "
            "but TMW_MCP_API_KEYS is empty. Set at least one key, or bind to "
            "127.0.0.1 for local-only use."
        )

    return Settings(
        db_server=_require("TMW_DB_SERVER"),
        db_database=_require("TMW_DB_DATABASE"),
        db_driver=_require("TMW_DB_DRIVER"),
        db_user=user,
        db_password=password,
        db_timeout=_int("TMW_DB_TIMEOUT", 30),
        db_encrypt=_optional("TMW_DB_ENCRYPT"),
        db_trust_server_certificate=_optional("TMW_DB_TRUST_SERVER_CERTIFICATE"),
        checkcall_lookback_days=_int("TMW_CHECKCALL_LOOKBACK_DAYS", 30),
        max_search_rows=_int("TMW_MAX_SEARCH_ROWS", 200),
        mcp_host=mcp_host,
        mcp_port=_int("TMW_MCP_PORT", 8000),
        mcp_allowed_hosts=_csv("TMW_MCP_ALLOWED_HOSTS"),
        mcp_allowed_origins=_csv("TMW_MCP_ALLOWED_ORIGINS"),
        api_keys=api_keys,
    )

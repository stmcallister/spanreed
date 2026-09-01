"""Application configuration, read from environment variables and validated at startup."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Every knob the application has.

    Field names map to upper case environment variables, so `anthropic_api_key`
    is read from ANTHROPIC_API_KEY. Optional integrations stay unset by default
    and the code paths that use them skip cleanly.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage
    db_path: Path = Path("/data/newsbrief.db")
    seed_path: Path = Path("seed/topics.yaml")
    item_retention_days: int = Field(default=60, ge=1)

    # Brief shape. The cap is enforced in code, never left to a prompt.
    max_brief_items: int = Field(default=15, ge=1)
    deep_read_items: int = Field(default=3, ge=0)
    render_timezone: str = "America/Los_Angeles"

    # LLM
    anthropic_api_key: str | None = None
    filter_model: str = "claude-haiku-4-5-20251001"
    write_model: str = "claude-sonnet-5"

    # HTTP
    user_agent: str = "newsbrief/1.0 (+https://github.com/stmcallister/newsbrief)"
    http_timeout_seconds: float = Field(default=15.0, gt=0)
    rsshub_base_url: str | None = None

    # Web UI
    web_host: str = "0.0.0.0"
    web_port: int = 8000
    basic_auth_user: str = "newsbrief"
    basic_auth_password: str | None = None

    # Email delivery, optional
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None

    # Logging
    log_level: str = "INFO"

    @property
    def llm_configured(self) -> bool:
        """True when the Anthropic SDK has a key to work with."""
        return bool(self.anthropic_api_key)

    @property
    def smtp_configured(self) -> bool:
        """True when email delivery has enough settings to attempt a send."""
        return bool(self.smtp_host and self.smtp_from and self.smtp_to)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process wide settings, parsed once."""
    return Settings()

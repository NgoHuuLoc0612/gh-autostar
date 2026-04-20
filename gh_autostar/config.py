"""
Centralised configuration for gh-autostar.

Token storage priority (highest → lowest):
  1. OS keychain (keyring) — most secure, used when available
  2. Environment variable GH_AUTOSTAR_GITHUB_TOKEN or GITHUB_TOKEN
  3. .env file (permissions enforced to 600)
  4. Empty string (will prompt on next command)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from platformdirs import user_config_dir, user_data_dir, user_log_dir
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_NAME = "gh-autostar"


def _config_dir() -> Path:
    p = Path(user_config_dir(_APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _data_dir() -> Path:
    p = Path(user_data_dir(_APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _log_dir() -> Path:
    p = Path(user_log_dir(_APP_NAME))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_token() -> str:
    """
    Resolve GitHub token from most-secure to least-secure source.
    1. OS keychain
    2. GH_AUTOSTAR_GITHUB_TOKEN env var
    3. GITHUB_TOKEN env var
    4. Empty string
    """
    from gh_autostar.security import load_token_keychain
    token = load_token_keychain()
    if token:
        return token
    token = os.environ.get("GH_AUTOSTAR_GITHUB_TOKEN", "")
    if token:
        return token
    token = os.environ.get("GITHUB_TOKEN", "")
    return token


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GH_AUTOSTAR_",
        env_file=str(_config_dir() / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── GitHub ───────────────────────────────────────────────────────────────
    # Stored as SecretStr — pydantic will never include the raw value in
    # __repr__, model_dump(), or logs.
    github_token: SecretStr = Field(
        default=SecretStr(""),
        description="Personal Access Token. Prefer OS keychain over .env.",
    )
    github_api_base: str = Field(
        default="https://api.github.com",
        description="GitHub API base URL (override for GHES).",
    )
    github_username: str = Field(
        default="",
        description="GitHub username (auto-detected when empty).",
    )

    # ── Token resolution ──────────────────────────────────────────────────────
    @model_validator(mode="after")
    def _resolve_token_from_keychain(self) -> "Settings":
        """
        If the token is empty in env/file, try keychain.
        This runs after pydantic loads from env/file.
        """
        raw = self.github_token.get_secret_value() if self.github_token else ""
        if not raw:
            from gh_autostar.security import load_token_keychain
            keychain_token = load_token_keychain()
            if keychain_token:
                object.__setattr__(self, "github_token", SecretStr(keychain_token))
        return self

    # ── Batch / Rate-limit ───────────────────────────────────────────────────
    batch_size: Annotated[int, Field(ge=1, le=200)] = Field(
        default=30,
        description="Repos to star per batch run.",
    )
    batch_delay_seconds: Annotated[float, Field(ge=0.0)] = Field(
        default=1.5,
        description="Delay between individual star requests (seconds).",
    )
    batch_cooldown_seconds: Annotated[float, Field(ge=0.0)] = Field(
        default=3.0,
        description="Cooldown after completing a full batch (seconds).",
    )
    rate_limit_buffer: Annotated[int, Field(ge=0, le=5000)] = Field(
        default=10,
        description="Halt when fewer than this many API requests remain.",
    )

    # ── HTTP ─────────────────────────────────────────────────────────────────
    http_timeout_seconds: Annotated[float, Field(ge=1.0)] = Field(default=30.0)
    http_max_retries: Annotated[int, Field(ge=0, le=10)] = Field(default=5)
    http_backoff_factor: Annotated[float, Field(ge=1.0)] = Field(default=2.0)

    # ── Scheduler / Daemon ───────────────────────────────────────────────────
    scheduler_enabled: bool = Field(default=True)
    scheduler_interval_minutes: Annotated[int, Field(ge=1)] = Field(default=60)
    run_on_startup: bool = Field(default=True)
    startup_delay_seconds: Annotated[float, Field(ge=0.0)] = Field(default=15.0)

    # ── Filtering ────────────────────────────────────────────────────────────
    min_stars: Annotated[int, Field(ge=0)] = Field(default=0)
    max_stars: int | None = Field(default=None)
    min_forks: Annotated[int, Field(ge=0)] = Field(default=0)
    languages: list[str] = Field(default_factory=list)
    exclude_forks: bool = Field(default=False)
    exclude_archived: bool = Field(default=True)
    exclude_private: bool = Field(default=True)
    require_topics: list[str] = Field(default_factory=list)
    any_topics: list[str] = Field(default_factory=list)
    exclude_owners: list[str] = Field(default_factory=list)

    # ── Discovery sources ─────────────────────────────────────────────────────
    sources: list[
        Literal[
            "trending",
            "explore",
            "random_popular",
            "recently_active",
            "following_starred",
            "topic_search",
            "manual_list",
        ]
    ] = Field(
        default=["trending", "random_popular", "recently_active"],
        description="Which discovery strategies to use.",
    )
    topic_search_terms: list[str] = Field(default_factory=list)
    manual_repos: list[str] = Field(default_factory=list)

    # ── Storage ───────────────────────────────────────────────────────────────
    database_path: Path = Field(
        default_factory=lambda: _data_dir() / "autostar.db",
    )
    cache_ttl_hours: Annotated[int, Field(ge=1)] = Field(default=6)

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_file: Path = Field(default_factory=lambda: _log_dir() / "autostar.log")
    log_max_bytes: int = Field(default=10 * 1024 * 1024)
    log_backup_count: int = Field(default=5)

    # ── Email digest / SMTP ──────────────────────────────────────────────────
    digest_enabled: bool = Field(default=False, description="Enable weekly email digest.")
    digest_recipients: list[str] = Field(
        default_factory=list,
        description="Email addresses to send digest to.",
    )
    digest_day_of_week: Literal["mon","tue","wed","thu","fri","sat","sun"] = Field(
        default="mon",
        description="Day of week to send digest.",
    )
    digest_hour_utc: Annotated[int, Field(ge=0, le=23)] = Field(
        default=8,
        description="Hour (UTC) to send digest.",
    )
    smtp_host: str = Field(default="smtp.gmail.com")
    smtp_port: int = Field(default=465)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_use_tls: bool = Field(default=True)
    smtp_use_starttls: bool = Field(default=False)
    smtp_from_addr: str = Field(default="")

    # ── Security ──────────────────────────────────────────────────────────────
    use_keychain: bool = Field(
        default=True,
        description="Store token in OS keychain instead of .env file.",
    )
    audit_log_enabled: bool = Field(
        default=True,
        description="Write security audit log for all star/auth events.",
    )

    # ── Anti-ban / Human behaviour ───────────────────────────────────────────
    respect_human_hours: bool = Field(
        default=True,
        description="Only run during active hours (avoids 24/7 bot pattern).",
    )
    active_hour_start: Annotated[int, Field(ge=0, le=23)] = Field(
        default=8,
        description="Start of active hours (24h, local time).",
    )
    active_hour_end: Annotated[int, Field(ge=1, le=24)] = Field(
        default=23,
        description="End of active hours (24h, local time).",
    )
    timezone_offset_hours: Annotated[int, Field(ge=-12, le=14)] = Field(
        default=7,
        description="Your UTC offset (e.g. 7 for UTC+7 Vietnam).",
    )
    daily_star_cap: Annotated[int, Field(ge=1, le=500)] = Field(
        default=150,
        description="Maximum stars per day (anti-abuse cap).",
    )
    hourly_star_cap: Annotated[int, Field(ge=1, le=100)] = Field(
        default=25,
        description="Maximum stars per hour (burst protection).",
    )
    jitter_factor: Annotated[float, Field(ge=0.0, le=2.0)] = Field(
        default=0.5,
        description="Randomness in delay (0=fixed, 1=high variance).",
    )
    think_time_probability: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.15,
        description="Probability of a longer 'reading' pause between stars.",
    )
    burst_cooldown_every: Annotated[int, Field(ge=1)] = Field(
        default=10,
        description="Take a longer break every N stars.",
    )
    burst_cooldown_min_seconds: Annotated[float, Field(ge=0.0)] = Field(
        default=15.0,
        description="Minimum burst cooldown duration (seconds).",
    )
    burst_cooldown_max_seconds: Annotated[float, Field(ge=0.0)] = Field(
        default=45.0,
        description="Maximum burst cooldown duration (seconds).",
    )
    pre_star_browse_probability: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.6,
        description="Probability of fetching repo details before starring (human browse simulation).",
    )
    weekend_slowdown: bool = Field(
        default=True,
        description="Reduce starring activity on weekends.",
    )
    weekend_slowdown_factor: Annotated[float, Field(ge=0.0, le=1.0)] = Field(
        default=0.4,
        description="On weekends, star with this probability per repo.",
    )

    # ── Notifications ─────────────────────────────────────────────────────────
    notify_on_completion: bool = Field(default=False)

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("languages", "require_topics", "any_topics", mode="before")
    @classmethod
    def _normalise_list(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [item.strip().lower() for item in v.split(",") if item.strip()]
        if isinstance(v, list):
            return [str(item).strip().lower() for item in v]
        return []

    @field_validator("manual_repos", mode="before")
    @classmethod
    def _validate_manual_repos(cls, v: object) -> list[str]:
        from gh_autostar.security import sanitise_repo_slug
        slugs: list[str] = []
        raw_list: list[str] = []
        if isinstance(v, str):
            raw_list = [x.strip() for x in v.split(",") if x.strip()]
        elif isinstance(v, list):
            raw_list = [str(x).strip() for x in v]
        for slug in raw_list:
            try:
                slugs.append(sanitise_repo_slug(slug))
            except ValueError:
                pass  # silently drop invalid slugs from config
        return slugs

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def token(self) -> str:
        """Return the raw token string (use sparingly — prefer SecretStr)."""
        if self.github_token:
            return self.github_token.get_secret_value()
        return ""

    @property
    def config_dir(self) -> Path:
        return _config_dir()

    @property
    def data_dir(self) -> Path:
        return _data_dir()

    @property
    def log_dir(self) -> Path:
        return _log_dir()

    def save_token(self, token: str) -> bool:
        """
        Save token to OS keychain if available, else to .env file.
        Returns True if keychain was used.
        """
        from gh_autostar.security import (
            secure_env_file, store_token_keychain, validate_token_format
        )
        if not validate_token_format(token):
            raise ValueError(
                "Token format not recognised. Expected ghp_*, github_pat_*, "
                "ghs_*, gho_*, or 40-char hex."
            )

        if self.use_keychain and store_token_keychain(token):
            # Remove token from .env if it was stored there before
            self._remove_from_env("github_token")
            return True

        # Fallback: .env with restricted permissions
        self.save_env(github_token=token)
        env_path = self.config_dir / ".env"
        secure_env_file(env_path)
        return False

    def save_env(self, **overrides: object) -> None:
        """Persist settings to .env file (never write token if keychain is on)."""
        from gh_autostar.security import mask_token, secure_env_file

        env_path = self.config_dir / ".env"
        lines: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    lines[k.strip()] = v.strip()

        for key, val in overrides.items():
            env_key = f"GH_AUTOSTAR_{key.upper()}"
            # Never write token to .env when keychain is enabled
            if key == "github_token" and self.use_keychain:
                continue
            if isinstance(val, SecretStr):
                lines[env_key] = val.get_secret_value()
            elif isinstance(val, list):
                lines[env_key] = ",".join(str(x) for x in val)
            elif isinstance(val, bool):
                lines[env_key] = "true" if val else "false"
            else:
                lines[env_key] = str(val)

        env_path.write_text(
            "\n".join(f"{k}={v}" for k, v in sorted(lines.items())) + "\n",
            encoding="utf-8",
        )
        secure_env_file(env_path)

    def _remove_from_env(self, key: str) -> None:
        """Remove a key from the .env file."""
        env_path = self.config_dir / ".env"
        if not env_path.exists():
            return
        env_key = f"GH_AUTOSTAR_{key.upper()}"
        lines = [
            line for line in env_path.read_text().splitlines()
            if not line.startswith(env_key + "=")
        ]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def security_warnings(self) -> list[str]:
        """Return a list of active security warnings."""
        from gh_autostar.security import check_env_file_permissions, load_token_keychain
        warnings: list[str] = []

        # Token in .env instead of keychain
        env_path = self.config_dir / ".env"
        if env_path.exists():
            content = env_path.read_text()
            if "GH_AUTOSTAR_GITHUB_TOKEN=" in content:
                tok_line = [l for l in content.splitlines() if l.startswith("GH_AUTOSTAR_GITHUB_TOKEN=")]
                if tok_line and tok_line[0].split("=", 1)[1].strip():
                    warnings.append(
                        "Token stored in plaintext .env file. "
                        "Run 'gh-autostar auth migrate-keychain' to move it to OS keychain."
                    )

        # File permission warnings
        warnings.extend(check_env_file_permissions(env_path))

        # No token at all
        if not self.token and not load_token_keychain():
            warnings.append("No GitHub token configured. Run 'gh-autostar auth login'.")

        return warnings


_settings_instance: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """Return the cached (or fresh) Settings singleton."""
    global _settings_instance
    if _settings_instance is None or reload:
        _settings_instance = Settings()
    return _settings_instance

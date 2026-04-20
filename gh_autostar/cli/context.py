"""
Shared CLI context object passed through Typer callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from gh_autostar.config import Settings, get_settings
from gh_autostar.core.client import GitHubClient
from gh_autostar.storage.database import Database


@dataclass
class AppContext:
    settings: Settings = field(default_factory=get_settings)
    _client: Optional[GitHubClient] = field(default=None, init=False, repr=False)
    _db: Optional[Database] = field(default=None, init=False, repr=False)

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = Database(self.settings.database_path)
        return self._db

    @property
    def client(self) -> GitHubClient:
        if self._client is None:
            cfg = self.settings
            self._client = GitHubClient(
                token=cfg.token,
                base_url=cfg.github_api_base,
                timeout=cfg.http_timeout_seconds,
                max_retries=cfg.http_max_retries,
                backoff_factor=cfg.http_backoff_factor,
                rate_limit_buffer=cfg.rate_limit_buffer,
            )
        return self._client

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

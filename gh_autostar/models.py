"""Domain models shared across the entire codebase."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class StarStatus(str, Enum):
    PENDING = "pending"
    STARRED = "starred"
    SKIPPED = "skipped"
    FAILED = "failed"
    ALREADY_STARRED = "already_starred"
    FILTERED_OUT = "filtered_out"


class DiscoverySource(str, Enum):
    TRENDING = "trending"
    EXPLORE = "explore"
    FOLLOWING_STARRED = "following_starred"
    TOPIC_SEARCH = "topic_search"
    MANUAL_LIST = "manual_list"


class Repository(BaseModel):
    """A GitHub repository as returned (or discovered) by the API."""

    id: int
    full_name: str  # "owner/repo"
    name: str
    owner: str
    description: str | None = None
    language: str | None = None
    stargazers_count: int = 0
    forks_count: int = 0
    open_issues_count: int = 0
    topics: list[str] = Field(default_factory=list)
    is_fork: bool = False
    is_archived: bool = False
    is_private: bool = False
    is_template: bool = False
    pushed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    html_url: str = ""
    clone_url: str = ""
    default_branch: str = "main"
    license_name: str | None = None

    @field_validator("full_name")
    @classmethod
    def _validate_full_name(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(f"full_name must be 'owner/repo', got: {v!r}")
        return v

    @model_validator(mode="before")
    @classmethod
    def _from_github_payload(cls, data: Any) -> Any:
        """Accept raw GitHub API JSON directly."""
        if not isinstance(data, dict):
            return data
        if "owner" in data and isinstance(data["owner"], dict):
            data["owner"] = data["owner"].get("login", "")
        if "license" in data and isinstance(data["license"], dict):
            data["license_name"] = (data["license"] or {}).get("name")
            del data["license"]
        # Flatten boolean flags
        data.setdefault("is_fork", data.pop("fork", False))
        data.setdefault("is_archived", data.pop("archived", False))
        data.setdefault("is_private", data.pop("private", False))
        data.setdefault("is_template", data.pop("is_template", False))
        # Topics come nested in an array
        if "topics" not in data:
            data["topics"] = []
        return data

    @property
    def slug(self) -> str:
        return self.full_name

    def matches_language(self, whitelist: list[str]) -> bool:
        if not whitelist:
            return True
        lang = (self.language or "").lower()
        return lang in [w.lower() for w in whitelist]

    def matches_topics(
        self, require: list[str], any_of: list[str]
    ) -> bool:
        repo_topics = {t.lower() for t in self.topics}
        if require and not all(t.lower() in repo_topics for t in require):
            return False
        if any_of and not any(t.lower() in repo_topics for t in any_of):
            return False
        return True


class StarRecord(BaseModel):
    """A persisted record of a star operation."""

    repo_full_name: str
    repo_id: int
    status: StarStatus
    source: DiscoverySource
    starred_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    error_message: str | None = None
    attempt_count: int = 1


class BatchResult(BaseModel):
    """Aggregated result of one batch run."""

    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    finished_at: datetime | None = None
    total_discovered: int = 0
    total_starred: int = 0
    total_skipped: int = 0
    total_failed: int = 0
    total_already_starred: int = 0
    total_filtered_out: int = 0
    records: list[StarRecord] = Field(default_factory=list)
    api_calls_used: int = 0
    api_calls_remaining: int | None = None

    def add_record(self, record: StarRecord) -> None:
        self.records.append(record)
        match record.status:
            case StarStatus.STARRED:
                self.total_starred += 1
            case StarStatus.SKIPPED:
                self.total_skipped += 1
            case StarStatus.FAILED:
                self.total_failed += 1
            case StarStatus.ALREADY_STARRED:
                self.total_already_starred += 1
            case StarStatus.FILTERED_OUT:
                self.total_filtered_out += 1

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    @property
    def success_rate(self) -> float:
        total = self.total_starred + self.total_failed
        return self.total_starred / total if total else 0.0


class RateLimit(BaseModel):
    limit: int
    remaining: int
    reset_at: datetime
    used: int

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def seconds_until_reset(self) -> float:
        delta = (self.reset_at - datetime.now(tz=timezone.utc)).total_seconds()
        return max(0.0, delta)


REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


def parse_repo_slug(slug: str) -> tuple[str, str]:
    """Return (owner, repo) from 'owner/repo' string."""
    slug = slug.strip()
    if not REPO_SLUG_RE.match(slug):
        raise ValueError(f"Invalid repo slug: {slug!r}")
    owner, _, repo = slug.partition("/")
    return owner, repo

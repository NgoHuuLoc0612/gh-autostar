"""Shared pytest fixtures for gh-autostar tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from gh_autostar.config import Settings
from gh_autostar.models import DiscoverySource, Repository, StarRecord, StarStatus
from gh_autostar.storage.database import Database


@pytest.fixture()
def tmp_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test_autostar.db")


@pytest.fixture()
def sample_repo() -> Repository:
    return Repository(
        id=12345,
        full_name="acme/widget",
        name="widget",
        owner="acme",
        description="A test repo",
        language="Python",
        stargazers_count=500,
        forks_count=42,
        topics=["python", "cli", "automation"],
        is_fork=False,
        is_archived=False,
        is_private=False,
        html_url="https://github.com/acme/widget",
    )


@pytest.fixture()
def sample_repo_fork() -> Repository:
    return Repository(
        id=99999,
        full_name="user/forked-widget",
        name="forked-widget",
        owner="user",
        language="Python",
        stargazers_count=5,
        forks_count=0,
        is_fork=True,
        is_archived=False,
        is_private=False,
        html_url="https://github.com/user/forked-widget",
    )


@pytest.fixture()
def sample_star_record(sample_repo: Repository) -> StarRecord:
    return StarRecord(
        repo_full_name=sample_repo.full_name,
        repo_id=sample_repo.id,
        status=StarStatus.STARRED,
        source=DiscoverySource.TRENDING,
    )


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        github_token=SecretStr("ghp_test_token_1234567890abcdefghij"),
        database_path=tmp_path / "test.db",
        batch_size=5,
        batch_delay_seconds=0.0,
        batch_cooldown_seconds=0.0,
        cache_ttl_hours=1,
        sources=["manual_list"],
        manual_repos=[],
        log_level="DEBUG",
        use_keychain=False,
        audit_log_enabled=False,
    )

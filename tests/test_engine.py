"""Tests for the AutoStarEngine (client is mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gh_autostar.core.engine import AutoStarEngine
from gh_autostar.models import BatchResult, DiscoverySource, Repository, StarStatus
from gh_autostar.storage.database import Database


def _make_mock_client(starred: set[str] | None = None) -> MagicMock:
    client = MagicMock()
    client.__enter__ = lambda s: s
    client.__exit__ = MagicMock(return_value=False)
    client.rate_limit = None
    client.get_starred_repo_names.return_value = starred or set()
    client.is_starred.return_value = False
    client.star_repo.return_value = None
    return client


def _make_repo(full_name: str, stars: int = 100) -> Repository:
    owner, name = full_name.split("/")
    return Repository(
        id=hash(full_name) % 100000,
        full_name=full_name,
        name=name,
        owner=owner,
        language="Python",
        stargazers_count=stars,
        forks_count=5,
        topics=["python"],
        is_fork=False,
        is_archived=False,
        is_private=False,
        html_url=f"https://github.com/{full_name}",
    )


class TestAutoStarEngine:
    def test_run_batch_stars_candidates(
        self, settings, tmp_db: Database
    ) -> None:
        settings.manual_repos = ["acme/alpha", "acme/beta"]
        settings.sources = ["manual_list"]

        client = _make_mock_client()
        client.get_repo.side_effect = lambda o, r: {
            "id": 1,
            "full_name": f"{o}/{r}",
            "name": r,
            "owner": {"login": o},
            "language": "Python",
            "stargazers_count": 100,
            "forks_count": 5,
            "topics": ["python"],
            "fork": False,
            "archived": False,
            "private": False,
            "is_template": False,
            "html_url": f"https://github.com/{o}/{r}",
            "clone_url": "",
            "default_branch": "main",
        }

        engine = AutoStarEngine(settings=settings, client=client, db=tmp_db)
        result = engine.run_batch(dry_run=False)

        assert isinstance(result, BatchResult)
        assert result.total_starred == 2
        assert result.total_failed == 0

    def test_run_batch_dry_run(self, settings, tmp_db: Database) -> None:
        settings.manual_repos = ["acme/gamma"]
        settings.sources = ["manual_list"]

        client = _make_mock_client()
        client.get_repo.return_value = {
            "id": 99,
            "full_name": "acme/gamma",
            "name": "gamma",
            "owner": {"login": "acme"},
            "language": "Go",
            "stargazers_count": 200,
            "forks_count": 3,
            "topics": [],
            "fork": False,
            "archived": False,
            "private": False,
            "is_template": False,
            "html_url": "https://github.com/acme/gamma",
            "clone_url": "",
            "default_branch": "main",
        }

        engine = AutoStarEngine(settings=settings, client=client, db=tmp_db)
        result = engine.run_batch(dry_run=True)

        # Dry-run: star_repo should NOT have been called
        client.star_repo.assert_not_called()
        assert result.total_starred == 1

    def test_already_starred_not_re_starred(self, settings, tmp_db: Database) -> None:
        settings.manual_repos = ["acme/delta"]
        settings.sources = ["manual_list"]

        # Seed the cache so acme/delta appears already starred
        client = _make_mock_client(starred={"acme/delta"})
        client.get_repo.return_value = {
            "id": 77,
            "full_name": "acme/delta",
            "name": "delta",
            "owner": {"login": "acme"},
            "language": "Rust",
            "stargazers_count": 50,
            "forks_count": 1,
            "topics": [],
            "fork": False,
            "archived": False,
            "private": False,
            "is_template": False,
            "html_url": "https://github.com/acme/delta",
            "clone_url": "",
            "default_branch": "main",
        }
        # Cache the starred names so get_starred_set returns them
        tmp_db.cache_starred_names({"acme/delta"}, ttl_hours=1)

        engine = AutoStarEngine(settings=settings, client=client, db=tmp_db)
        result = engine.run_batch(dry_run=False)

        client.star_repo.assert_not_called()
        assert result.total_starred == 0

    def test_api_error_marks_failed(self, settings, tmp_db: Database) -> None:
        from gh_autostar.core.client import GitHubAPIError

        settings.manual_repos = ["acme/epsilon"]
        settings.sources = ["manual_list"]

        client = _make_mock_client()
        client.get_repo.return_value = {
            "id": 55,
            "full_name": "acme/epsilon",
            "name": "epsilon",
            "owner": {"login": "acme"},
            "language": "Python",
            "stargazers_count": 300,
            "forks_count": 20,
            "topics": [],
            "fork": False,
            "archived": False,
            "private": False,
            "is_template": False,
            "html_url": "https://github.com/acme/epsilon",
            "clone_url": "",
            "default_branch": "main",
        }
        client.star_repo.side_effect = GitHubAPIError(403, "Forbidden", "url")

        engine = AutoStarEngine(settings=settings, client=client, db=tmp_db)
        result = engine.run_batch(dry_run=False)

        assert result.total_failed == 1
        assert result.total_starred == 0

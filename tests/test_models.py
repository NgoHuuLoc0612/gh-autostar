"""Tests for domain models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gh_autostar.models import Repository, StarRecord, StarStatus, parse_repo_slug


class TestRepository:
    def test_from_github_payload(self) -> None:
        payload = {
            "id": 1,
            "full_name": "owner/repo",
            "name": "repo",
            "owner": {"login": "owner", "id": 42},
            "description": "desc",
            "language": "Python",
            "stargazers_count": 100,
            "forks_count": 10,
            "topics": ["python", "cli"],
            "fork": False,
            "archived": False,
            "private": False,
            "is_template": False,
            "html_url": "https://github.com/owner/repo",
            "clone_url": "https://github.com/owner/repo.git",
            "default_branch": "main",
            "license": {"name": "MIT"},
        }
        repo = Repository.model_validate(payload)
        assert repo.full_name == "owner/repo"
        assert repo.owner == "owner"
        assert repo.license_name == "MIT"
        assert not repo.is_fork

    def test_invalid_full_name(self) -> None:
        with pytest.raises(ValidationError):
            Repository(
                id=1,
                full_name="invalid-no-slash",
                name="repo",
                owner="owner",
            )

    def test_language_filter(self, sample_repo: Repository) -> None:
        assert sample_repo.matches_language(["python"])
        assert sample_repo.matches_language(["Python"])
        assert not sample_repo.matches_language(["rust"])
        assert sample_repo.matches_language([])  # empty = all

    def test_topic_filter(self, sample_repo: Repository) -> None:
        assert sample_repo.matches_topics(["python"], [])
        assert sample_repo.matches_topics([], ["cli"])
        assert not sample_repo.matches_topics(["rust"], [])
        assert not sample_repo.matches_topics([], ["rust"])
        assert sample_repo.matches_topics(["python", "cli"], [])  # all required


class TestParseRepoSlug:
    def test_valid(self) -> None:
        owner, repo = parse_repo_slug("microsoft/vscode")
        assert owner == "microsoft"
        assert repo == "vscode"

    def test_with_dots(self) -> None:
        owner, repo = parse_repo_slug("openai/gpt-4.1")
        assert owner == "openai"

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            parse_repo_slug("not-a-slug")

    def test_strips_whitespace(self) -> None:
        owner, repo = parse_repo_slug("  owner/repo  ")
        assert owner == "owner"
        assert repo == "repo"


class TestStarRecord:
    def test_creation(self, sample_star_record: StarRecord) -> None:
        assert sample_star_record.status == StarStatus.STARRED
        assert sample_star_record.attempt_count == 1

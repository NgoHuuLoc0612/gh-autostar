"""Tests for the filter pipeline."""

from __future__ import annotations

import pytest

from gh_autostar.core.filters import (
    already_starred_filter,
    build_filters,
    exclude_owners_filter,
    filter_pipeline,
    language_filter,
    max_stars_filter,
    min_forks_filter,
    min_stars_filter,
    not_archived_filter,
    not_fork_filter,
    not_private_filter,
    topic_filter,
)
from gh_autostar.models import Repository


def _make_repo(**kwargs) -> Repository:
    defaults = dict(
        id=1,
        full_name="owner/repo",
        name="repo",
        owner="owner",
        language="Python",
        stargazers_count=100,
        forks_count=10,
        topics=["python"],
        is_fork=False,
        is_archived=False,
        is_private=False,
    )
    defaults.update(kwargs)
    return Repository(**defaults)


class TestIndividualFilters:
    def test_min_stars(self) -> None:
        f = min_stars_filter(50)
        assert f(_make_repo(stargazers_count=100))
        assert not f(_make_repo(stargazers_count=49))

    def test_max_stars(self) -> None:
        f = max_stars_filter(200)
        assert f(_make_repo(stargazers_count=200))
        assert not f(_make_repo(stargazers_count=201))

    def test_min_forks(self) -> None:
        f = min_forks_filter(5)
        assert f(_make_repo(forks_count=10))
        assert not f(_make_repo(forks_count=3))

    def test_language(self) -> None:
        f = language_filter(["python", "rust"])
        assert f(_make_repo(language="Python"))
        assert f(_make_repo(language="rust"))
        assert not f(_make_repo(language="Go"))
        assert not f(_make_repo(language="anything"))
        # with empty whitelist, all languages pass:
        f_empty = language_filter([])
        assert f_empty(_make_repo(language="COBOL"))

    def test_not_fork(self) -> None:
        f = not_fork_filter()
        assert f(_make_repo(is_fork=False))
        assert not f(_make_repo(is_fork=True))

    def test_not_archived(self) -> None:
        f = not_archived_filter()
        assert f(_make_repo(is_archived=False))
        assert not f(_make_repo(is_archived=True))

    def test_not_private(self) -> None:
        f = not_private_filter()
        assert f(_make_repo(is_private=False))
        assert not f(_make_repo(is_private=True))

    def test_topic_require(self) -> None:
        f = topic_filter(require=["python"], any_of=[])
        assert f(_make_repo(topics=["python", "cli"]))
        assert not f(_make_repo(topics=["rust"]))

    def test_topic_any_of(self) -> None:
        f = topic_filter(require=[], any_of=["python", "rust"])
        assert f(_make_repo(topics=["python"]))
        assert f(_make_repo(topics=["rust"]))
        assert not f(_make_repo(topics=["java"]))

    def test_exclude_owners(self) -> None:
        f = exclude_owners_filter(["spammer", "bot"])
        assert f(_make_repo(owner="gooduser", full_name="gooduser/repo"))
        assert not f(_make_repo(owner="Spammer", full_name="Spammer/repo"))  # case-insensitive

    def test_already_starred(self) -> None:
        starred = {"owner/repo", "other/lib"}
        f = already_starred_filter(starred)
        assert not f(_make_repo(full_name="owner/repo"))
        assert f(_make_repo(full_name="new/stuff", name="stuff", owner="new"))


class TestFilterPipeline:
    def test_all_pass(self) -> None:
        repos = [_make_repo(stargazers_count=i * 10) for i in range(1, 6)]
        filters = [min_stars_filter(0)]
        result = list(filter_pipeline(repos, filters))
        assert len(result) == 5

    def test_some_filtered(self) -> None:
        repos = [
            _make_repo(full_name="a/a", name="a", owner="a", stargazers_count=500),
            _make_repo(full_name="b/b", name="b", owner="b", stargazers_count=10),
            _make_repo(full_name="c/c", name="c", owner="c", stargazers_count=1000, is_fork=True),
        ]
        filters = [min_stars_filter(100), not_fork_filter()]
        result = list(filter_pipeline(repos, filters))
        assert len(result) == 1
        assert result[0].full_name == "a/a"

    def test_empty_filters(self) -> None:
        repos = [_make_repo()]
        result = list(filter_pipeline(repos, []))
        assert len(result) == 1


class TestBuildFilters:
    def test_no_filters(self) -> None:
        filters = build_filters()
        assert isinstance(filters, list)

    def test_full_filter_set(self) -> None:
        filters = build_filters(
            min_stars=10,
            max_stars=1000,
            min_forks=2,
            languages=["python"],
            exclude_forks=True,
            exclude_archived=True,
            exclude_private=True,
            require_topics=["python"],
            any_topics=["cli"],
            exclude_owners=["badactor"],
            starred_repos={"already/starred"},
            seen_repos={"already/seen"},
        )
        assert len(filters) >= 8

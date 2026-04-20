"""
Composable filter pipeline for discovered repositories.

Filters are applied in order; any False from any filter drops the repo.
"""

from __future__ import annotations

from typing import Callable, Iterable, Iterator

from gh_autostar.logging_setup import get_logger
from gh_autostar.models import Repository

logger = get_logger("filters")

Filter = Callable[[Repository], bool]


def filter_pipeline(
    repos: Iterable[Repository],
    filters: list[Filter],
) -> Iterator[Repository]:
    for repo in repos:
        passed = all(f(repo) for f in filters)
        if not passed:
            logger.debug("Filtered out %s", repo.full_name)
        else:
            yield repo


# ── Built-in filters ─────────────────────────────────────────────────────────

def min_stars_filter(threshold: int) -> Filter:
    def _f(r: Repository) -> bool:
        return r.stargazers_count >= threshold
    _f.__name__ = f"min_stars>={threshold}"
    return _f


def max_stars_filter(threshold: int) -> Filter:
    def _f(r: Repository) -> bool:
        return r.stargazers_count <= threshold
    _f.__name__ = f"max_stars<={threshold}"
    return _f


def min_forks_filter(threshold: int) -> Filter:
    def _f(r: Repository) -> bool:
        return r.forks_count >= threshold
    _f.__name__ = f"min_forks>={threshold}"
    return _f


def language_filter(languages: list[str]) -> Filter:
    def _f(r: Repository) -> bool:
        return r.matches_language(languages)
    _f.__name__ = f"language in {languages}"
    return _f


def not_fork_filter() -> Filter:
    def _f(r: Repository) -> bool:
        return not r.is_fork
    _f.__name__ = "not_fork"
    return _f


def not_archived_filter() -> Filter:
    def _f(r: Repository) -> bool:
        return not r.is_archived
    _f.__name__ = "not_archived"
    return _f


def not_private_filter() -> Filter:
    def _f(r: Repository) -> bool:
        return not r.is_private
    _f.__name__ = "not_private"
    return _f


def topic_filter(require: list[str], any_of: list[str]) -> Filter:
    def _f(r: Repository) -> bool:
        return r.matches_topics(require, any_of)
    _f.__name__ = f"topics(require={require}, any={any_of})"
    return _f


def exclude_owners_filter(owners: list[str]) -> Filter:
    blocked = {o.lower() for o in owners}
    def _f(r: Repository) -> bool:
        return r.owner.lower() not in blocked
    _f.__name__ = f"exclude_owners={owners}"
    return _f


def already_starred_filter(starred_set: set[str]) -> Filter:
    """Exclude repos the user has already starred."""
    def _f(r: Repository) -> bool:
        return r.full_name not in starred_set
    _f.__name__ = "not_already_starred"
    return _f


def already_seen_filter(seen_set: set[str]) -> Filter:
    """Exclude repos we have already processed this session."""
    def _f(r: Repository) -> bool:
        return r.full_name not in seen_set
    _f.__name__ = "not_seen_this_session"
    return _f


def build_filters(
    *,
    min_stars: int = 0,
    max_stars: int | None = None,
    min_forks: int = 0,
    languages: list[str] | None = None,
    exclude_forks: bool = False,
    exclude_archived: bool = True,
    exclude_private: bool = True,
    require_topics: list[str] | None = None,
    any_topics: list[str] | None = None,
    exclude_owners: list[str] | None = None,
    starred_repos: set[str] | None = None,
    seen_repos: set[str] | None = None,
) -> list[Filter]:
    filters: list[Filter] = []

    if min_stars > 0:
        filters.append(min_stars_filter(min_stars))
    if max_stars is not None:
        filters.append(max_stars_filter(max_stars))
    if min_forks > 0:
        filters.append(min_forks_filter(min_forks))
    if languages:
        filters.append(language_filter(languages))
    if exclude_forks:
        filters.append(not_fork_filter())
    if exclude_archived:
        filters.append(not_archived_filter())
    if exclude_private:
        filters.append(not_private_filter())
    if require_topics or any_topics:
        filters.append(topic_filter(require_topics or [], any_topics or []))
    if exclude_owners:
        filters.append(exclude_owners_filter(exclude_owners))
    if starred_repos is not None:
        filters.append(already_starred_filter(starred_repos))
    if seen_repos is not None:
        filters.append(already_seen_filter(seen_repos))

    return filters

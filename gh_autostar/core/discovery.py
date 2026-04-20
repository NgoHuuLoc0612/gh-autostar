"""
Repo discovery strategies.

CRITICAL — GitHub Search API + Fine-grained PAT requirement:
  Fine-grained PATs REQUIRE at least one non-qualifier text search term.
  Qualifier-only queries like "stars:>=100 language:python" return 422.
  Every query MUST include a keyword text term, e.g.:
    "python stars:>=100"              ✓
    "machine learning stars:>=1000"   ✓
    "stars:>=100 language:python"     ✗  422
    "is:public stars:>=50"            ✗  422
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Iterator, Protocol

from gh_autostar.core.client import GitHubClient
from gh_autostar.logging_setup import get_logger
from gh_autostar.models import DiscoverySource, Repository

logger = get_logger("discovery")


class DiscoveryStrategy(Protocol):
    source: DiscoverySource

    def discover(self, client: GitHubClient) -> Iterator[Repository]:
        ...


# ── Helpers ───────────────────────────────────────────────────────────────────

def _payload_to_repo(data: dict) -> Repository | None:
    try:
        return Repository.model_validate(data)
    except Exception as exc:
        logger.debug("Skipping invalid repo payload: %s", exc)
        return None


def _days_ago(n: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _search(
    client: GitHubClient,
    query: str,
    n: int,
    seen: set[str],
    sort: str = "stars",
) -> Iterator[Repository]:
    try:
        for payload in client.search_repos(
            query=query, sort=sort, order="desc", per_page=min(n, 30), max_results=n
        ):
            repo = _payload_to_repo(payload)
            if repo and repo.full_name not in seen:
                seen.add(repo.full_name)
                yield repo
    except Exception as exc:
        logger.warning("Search query failed %r: %s", query, exc)


# ── Query banks — all have at least one text keyword ─────────────────────────

# (text_term, optional_language_qualifier)
_KEYWORD_QUERIES = [
    # Generic programming terms
    ("library", ""),
    ("framework", ""),
    ("toolkit", ""),
    ("boilerplate", ""),
    ("template", ""),
    ("starter", ""),
    ("generator", ""),
    ("compiler", ""),
    ("interpreter", ""),
    ("debugger", ""),
    ("profiler", ""),
    ("linter", ""),
    ("formatter", ""),
    ("bundler", ""),
    ("parser", ""),
    ("serializer", ""),
    ("validator", ""),
    ("router", ""),
    ("scheduler", ""),
    ("crawler", ""),
    # By domain
    ("machine learning", ""),
    ("deep learning", ""),
    ("neural network", ""),
    ("computer vision", ""),
    ("natural language", ""),
    ("data pipeline", ""),
    ("data visualization", ""),
    ("web scraper", ""),
    ("api client", ""),
    ("rest api", ""),
    ("graphql", ""),
    ("microservice", ""),
    ("message queue", ""),
    ("event driven", ""),
    ("workflow automation", ""),
    ("infrastructure as code", ""),
    ("monitoring dashboard", ""),
    ("log aggregation", ""),
    ("search engine", ""),
    ("recommendation system", ""),
    # Language-specific terms (with language qualifier for precision)
    ("async runtime", "rust"),
    ("web framework", "rust"),
    ("cli tool", "rust"),
    ("web framework", "python"),
    ("async", "python"),
    ("data science", "python"),
    ("web framework", "go"),
    ("cli", "go"),
    ("concurrency", "go"),
    ("react", "javascript"),
    ("vue", "javascript"),
    ("state management", "typescript"),
    ("testing", "typescript"),
    ("android", "kotlin"),
    ("ios", "swift"),
    ("game engine", "c++"),
    ("embedded", "c"),
    ("functional", "haskell"),
    ("distributed", "elixir"),
    ("full stack", "javascript"),
    ("serverless", ""),
    ("container", ""),
    ("kubernetes operator", "go"),
    ("llm", "python"),
    ("rag", "python"),
    ("agent", "python"),
    ("nvim plugin", "lua"),
    ("zsh plugin", ""),
    ("dotfiles", ""),
    ("terminal emulator", ""),
    ("text editor", ""),
]

_SORT_OPTIONS = ["stars", "updated", "help-wanted-issues"]


# ── Strategies ────────────────────────────────────────────────────────────────

class TrendingStrategy:
    """
    Trending repos: keyword query + pushed:>DATE filter.
    Always includes a text keyword to satisfy fine-grained PAT requirement.
    """
    source = DiscoverySource.TRENDING

    def __init__(self, languages: list[str], count: int = 30) -> None:
        self._languages = [l for l in languages if l]
        self._count = count

    def discover(self, client: GitHubClient) -> Iterator[Repository]:
        seen: set[str] = set()
        pushed_date = _days_ago(random.choice([7, 14, 30, 60, 90]))
        min_stars = random.choice([10, 25, 50, 100, 200])

        if self._languages:
            per = max(10, self._count // min(3, len(self._languages)))
            for lang in self._languages[:3]:
                kw, _ = random.choice(_KEYWORD_QUERIES)
                q = f"{kw} language:{lang} stars:>={min_stars} pushed:>{pushed_date}"
                logger.info("Trending query: %s", q)
                yield from _search(client, q, per, seen, sort="stars")
        else:
            kw, lang_qual = random.choice(_KEYWORD_QUERIES)
            q = f"{kw} stars:>={min_stars} pushed:>{pushed_date}"
            if lang_qual:
                q += f" language:{lang_qual}"
            logger.info("Trending query: %s", q)
            yield from _search(client, q, self._count, seen, sort="stars")


class ExploreStrategy:
    """Random keyword + stars queries across different domains."""
    source = DiscoverySource.EXPLORE

    def __init__(self, count: int = 30) -> None:
        self._count = count

    def discover(self, client: GitHubClient) -> Iterator[Repository]:
        seen: set[str] = set()
        selections = random.sample(_KEYWORD_QUERIES, min(3, len(_KEYWORD_QUERIES)))
        per = max(10, self._count // len(selections))

        for kw, lang_qual in selections:
            min_stars = random.choice([50, 100, 200, 500, 1000])
            sort = random.choice(_SORT_OPTIONS)
            q = f"{kw} stars:>={min_stars}"
            if lang_qual:
                q += f" language:{lang_qual}"
            logger.info("Explore query: %s (sort=%s)", q, sort)
            yield from _search(client, q, per, seen, sort=sort)


class RandomPopularStrategy:
    """
    Randomised keyword + language + stars. Maximum variety across runs.
    """
    source = DiscoverySource.EXPLORE

    def __init__(self, count: int = 30) -> None:
        self._count = count

    def discover(self, client: GitHubClient) -> Iterator[Repository]:
        seen: set[str] = set()
        kw, lang_qual = random.choice(_KEYWORD_QUERIES)
        min_stars = random.choice([10, 25, 50, 100, 200, 500, 1000, 5000])
        sort = random.choice(_SORT_OPTIONS)
        q = f"{kw} stars:>={min_stars}"
        if lang_qual:
            q += f" language:{lang_qual}"
        logger.info("Random popular query: %s (sort=%s)", q, sort)
        yield from _search(client, q, self._count, seen, sort=sort)


class RecentlyActiveStrategy:
    """
    Repos with recent commits — good for finding hidden gems.
    """
    source = DiscoverySource.EXPLORE

    def __init__(self, count: int = 30) -> None:
        self._count = count

    def discover(self, client: GitHubClient) -> Iterator[Repository]:
        seen: set[str] = set()
        kw, lang_qual = random.choice(_KEYWORD_QUERIES)
        pushed_date = _days_ago(random.choice([3, 7, 14, 30]))
        min_stars = random.choice([5, 10, 25, 50])
        q = f"{kw} stars:>={min_stars} pushed:>{pushed_date}"
        if lang_qual:
            q += f" language:{lang_qual}"
        logger.info("Recently-active query: %s", q)
        yield from _search(client, q, self._count, seen, sort="updated")


class FollowingStarredStrategy:
    """Repos starred by people you follow."""
    source = DiscoverySource.FOLLOWING_STARRED

    def __init__(self, max_users: int = 10, repos_per_user: int = 20) -> None:
        self._max_users = max_users
        self._repos_per_user = repos_per_user

    def discover(self, client: GitHubClient) -> Iterator[Repository]:
        seen: set[str] = set()
        try:
            users = list(client.get_following())
        except Exception as exc:
            logger.warning("Could not fetch following list: %s", exc)
            return

        if not users:
            logger.info("Not following anyone — skipping following_starred.")
            return

        random.shuffle(users)
        sampled = users[: self._max_users]
        logger.info("Sampling starred repos from %d/%d followed users.", len(sampled), len(users))
        for user_payload in sampled:
            username = user_payload.get("login", "")
            if not username:
                continue
            count = 0
            try:
                for payload in client.get_user_starred(username, per_page=self._repos_per_user):
                    if count >= self._repos_per_user:
                        break
                    repo = _payload_to_repo(payload)
                    if repo and repo.full_name not in seen:
                        seen.add(repo.full_name)
                        yield repo
                        count += 1
            except Exception as exc:
                logger.warning("Could not fetch starred repos for %s: %s", username, exc)


class TopicSearchStrategy:
    """User-supplied search terms."""
    source = DiscoverySource.TOPIC_SEARCH

    def __init__(self, terms: list[str], per_term: int = 25) -> None:
        self._terms = terms
        self._per_term = per_term

    def discover(self, client: GitHubClient) -> Iterator[Repository]:
        seen: set[str] = set()
        for term in self._terms:
            # Use term as text keyword directly (it's user-supplied, already has text)
            q = term if ":" in term else f"{term} stars:>=10"
            logger.info("Topic search: %s", q)
            yield from _search(client, q, self._per_term, seen, sort="stars")


class ManualListStrategy:
    """Star explicit owner/repo slugs."""
    source = DiscoverySource.MANUAL_LIST

    def __init__(self, slugs: list[str]) -> None:
        self._slugs = slugs

    def discover(self, client: GitHubClient) -> Iterator[Repository]:
        for slug in self._slugs:
            try:
                owner, repo_name = slug.split("/", 1)
                payload = client.get_repo(owner, repo_name)
                repo = _payload_to_repo(payload)
                if repo:
                    yield repo
            except Exception as exc:
                logger.warning("Failed to fetch manual repo %r: %s", slug, exc)


# ── Builder ───────────────────────────────────────────────────────────────────

def build_strategies(
    sources: list[str],
    languages: list[str],
    topic_search_terms: list[str],
    manual_repos: list[str],
) -> list[DiscoveryStrategy]:
    mapping: dict[str, DiscoveryStrategy] = {
        "trending":          TrendingStrategy(languages=languages),
        "explore":           ExploreStrategy(),
        "random_popular":    RandomPopularStrategy(),
        "recently_active":   RecentlyActiveStrategy(),
        "following_starred": FollowingStarredStrategy(),
        "topic_search":      TopicSearchStrategy(terms=topic_search_terms),
        "manual_list":       ManualListStrategy(slugs=manual_repos),
    }
    return [mapping[s] for s in sources if s in mapping]

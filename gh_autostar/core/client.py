"""
Low-level GitHub API client.

Handles:
  - Auth via Bearer token
  - Separate rate-limit tracking for core (5000/hr) vs search (30/min)
  - Automatic retry with exponential back-off (tenacity)
  - Pagination via Link header
  - Clean 422 error messages for bad search queries
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Generator, Iterator

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from gh_autostar.logging_setup import get_logger
from gh_autostar.models import RateLimit

logger = get_logger("client")

_GITHUB_API_VERSION = "2022-11-28"


class RateLimitExceeded(Exception):
    def __init__(self, rate_limit: RateLimit) -> None:
        self.rate_limit = rate_limit
        super().__init__(
            f"GitHub API rate limit exhausted. "
            f"Resets in {rate_limit.seconds_until_reset:.0f}s "
            f"at {rate_limit.reset_at.isoformat()}"
        )


class GitHubAPIError(Exception):
    def __init__(self, status: int, message: str, url: str) -> None:
        self.status = status
        self.url = url
        super().__init__(f"GitHub API {status} for {url}: {message}")


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    if isinstance(exc, GitHubAPIError):
        return exc.status in {429, 500, 502, 503, 504}
    return False


class GitHubClient:
    """Authenticated, rate-limit-aware GitHub REST API client."""

    def __init__(
        self,
        token: str,  # raw value only — caller must unwrap SecretStr
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff_factor: float = 2.0,
        rate_limit_buffer: int = 10,
    ) -> None:
        if not token:
            raise ValueError("GitHub token is required. Set GH_AUTOSTAR_GITHUB_TOKEN.")
        self._base = base_url.rstrip("/")
        self._rate_limit_buffer = rate_limit_buffer
        # Track core and search limits separately
        self._core_limit: RateLimit | None = None
        self._search_limit: RateLimit | None = None
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor

        self._client = httpx.Client(
            base_url=self._base,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": _GITHUB_API_VERSION,
                "User-Agent": __import__("gh_autostar.antiban", fromlist=["session_user_agent"]).session_user_agent(),
            },
            timeout=timeout,
            follow_redirects=True,
        )

    # ── Public interface ─────────────────────────────────────────────────────

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def rate_limit(self) -> RateLimit | None:
        return self._core_limit

    def get_authenticated_user(self) -> dict[str, Any]:
        return self._get("/user")

    def get_rate_limit(self) -> dict[str, Any]:
        data = self._get("/rate_limit")
        resources = data.get("resources", {})
        if "core" in resources:
            self._update_limit_from_payload(resources["core"], is_search=False)
        if "search" in resources:
            self._update_limit_from_payload(resources["search"], is_search=True)
        return data

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return self._get(f"/repos/{owner}/{repo}")

    def is_starred(self, owner: str, repo: str) -> bool:
        try:
            self._request("GET", f"/user/starred/{owner}/{repo}", expect_body=False)
            return True
        except GitHubAPIError as exc:
            if exc.status == 404:
                return False
            raise

    def star_repo(self, owner: str, repo: str) -> None:
        self._request("PUT", f"/user/starred/{owner}/{repo}", expect_body=False)
        logger.debug("Starred %s/%s", owner, repo)

    def unstar_repo(self, owner: str, repo: str) -> None:
        self._request("DELETE", f"/user/starred/{owner}/{repo}", expect_body=False)
        logger.debug("Unstarred %s/%s", owner, repo)

    def get_starred_repos(self, per_page: int = 100) -> Iterator[dict[str, Any]]:
        yield from self._paginate("/user/starred", per_page=per_page)

    def get_starred_repo_names(self) -> set[str]:
        return {r["full_name"] for r in self.get_starred_repos()}

    def search_repos(
        self,
        query: str,
        sort: str = "stars",
        order: str = "desc",
        per_page: int = 30,
        max_results: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """
        Search repos via /search/repositories.
        Caps per_page at 30 (search API max before secondary rate limits).
        max_results defaults to 100 — single page only by default, keeps
        search quota usage low (30 req/min across all search calls).
        """
        # Search API returns max 100/page but recommends ≤30 to avoid secondary limits
        safe_per_page = min(per_page, 30)
        fetched = 0
        for item in self._paginate(
            "/search/repositories",
            per_page=safe_per_page,
            extra_params={"q": query, "sort": sort, "order": order},
            is_search=True,
        ):
            if fetched >= max_results:
                break
            yield item
            fetched += 1

    def get_following(self) -> Iterator[dict[str, Any]]:
        yield from self._paginate("/user/following", per_page=100)

    def get_user_starred(self, username: str, per_page: int = 30) -> Iterator[dict[str, Any]]:
        yield from self._paginate(f"/users/{username}/starred", per_page=per_page)

    # ── Private helpers ──────────────────────────────────────────────────────

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._request("GET", path, params=params)
        return resp.json()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        expect_body: bool = True,
        is_search: bool = False,
    ) -> httpx.Response:
        self._check_rate_limit(is_search=is_search)

        @retry(
            retry=retry_if_exception(_is_retryable),
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=self._backoff_factor, min=1, max=60),
            reraise=True,
        )
        def _do() -> httpx.Response:
            r = self._client.request(method, path, params=params, json=json)
            self._update_limit_from_headers(r.headers)

            if r.status_code == 429:
                retry_after = int(r.headers.get("retry-after", "60"))
                logger.warning("Secondary rate limit hit. Sleeping %ds.", retry_after)
                time.sleep(retry_after)
                r.raise_for_status()

            if r.status_code == 401:
                raise GitHubAPIError(
                    401,
                    "Bad credentials — your GitHub token is invalid or expired. "
                    "Run 'gh-autostar auth login' to set a new token.",
                    str(r.url),
                )

            if r.status_code == 304:
                return r

            if r.status_code == 422:
                # Unprocessable — bad query syntax; log and raise without retrying
                try:
                    body = r.json()
                    msg = body.get("message", "") + " " + str(body.get("errors", ""))
                except Exception:
                    msg = r.text
                raise GitHubAPIError(422, msg.strip(), str(r.url))

            if r.status_code >= 400:
                try:
                    body = r.json()
                    msg = body.get("message", r.text)
                except Exception:
                    msg = r.text
                raise GitHubAPIError(r.status_code, msg, str(r.url))

            return r

        try:
            return _do()
        except RetryError as exc:
            raise exc.last_attempt.exception() from exc  # type: ignore[arg-type]

    def _paginate(
        self,
        path: str,
        per_page: int = 30,
        extra_params: dict[str, Any] | None = None,
        is_search: bool = False,
    ) -> Generator[dict[str, Any], None, None]:
        params: dict[str, Any] = {"per_page": per_page, **(extra_params or {})}
        url: str | None = path
        while url:
            resp = self._request("GET", url, params=params, is_search=is_search)
            params = {}  # subsequent pages use full URL from Link header
            data = resp.json()

            if isinstance(data, dict) and "items" in data:
                items = data["items"]
            elif isinstance(data, list):
                items = data
            else:
                items = []

            yield from items

            url = self._next_link(resp.headers.get("link", ""))

    @staticmethod
    def _next_link(link_header: str) -> str | None:
        if not link_header:
            return None
        for part in link_header.split(","):
            url_part, *rel_parts = part.strip().split(";")
            rel = " ".join(rel_parts)
            if 'rel="next"' in rel:
                return url_part.strip().strip("<>")
        return None

    def _update_limit_from_headers(self, headers: httpx.Headers) -> None:
        try:
            limit = int(headers.get("x-ratelimit-limit", 0))
            remaining = int(headers.get("x-ratelimit-remaining", 0))
            reset_ts = int(headers.get("x-ratelimit-reset", 0))
            used = int(headers.get("x-ratelimit-used", 0))
            resource = headers.get("x-ratelimit-resource", "core")
            if not limit:
                return
            rl = RateLimit(
                limit=limit,
                remaining=remaining,
                reset_at=datetime.fromtimestamp(reset_ts, tz=timezone.utc),
                used=used,
            )
            if resource == "search":
                self._search_limit = rl
            else:
                self._core_limit = rl
        except (ValueError, TypeError):
            pass

    def _update_limit_from_payload(self, data: dict[str, Any], is_search: bool = False) -> None:
        try:
            rl = RateLimit(
                limit=data["limit"],
                remaining=data["remaining"],
                reset_at=datetime.fromtimestamp(data["reset"], tz=timezone.utc),
                used=data["used"],
            )
            if is_search:
                self._search_limit = rl
            else:
                self._core_limit = rl
        except (KeyError, TypeError):
            pass

    def _check_rate_limit(self, is_search: bool = False) -> None:
        rl = self._search_limit if is_search else self._core_limit
        if rl is None:
            return

        # For search: buffer is always at least 2 (out of 30/min)
        buffer = max(2, self._rate_limit_buffer // 20) if is_search else self._rate_limit_buffer

        if rl.remaining <= buffer:
            wait = rl.seconds_until_reset + 2
            kind = "Search" if is_search else "Core"
            logger.warning(
                "%s API rate limit buffer hit (%d/%d remaining). "
                "Sleeping %.0fs until reset.",
                kind, rl.remaining, rl.limit, wait,
            )
            time.sleep(wait)
            if is_search:
                self._search_limit = None
            else:
                self._core_limit = None
        elif rl.remaining <= rl.limit * 0.15:
            logger.warning(
                "Rate limit low: %d/%d remaining. Resets in %.0fs.",
                rl.remaining, rl.limit, rl.seconds_until_reset,
            )

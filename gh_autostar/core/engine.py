"""
AutoStar engine — the heart of gh-autostar.

Orchestrates:
  1. Anti-ban checks    — human hours, daily/hourly caps
  2. Discovery          — fetch candidate repos from configured sources
  3. Cache              — skip repos in recent cache hits
  4. Filtering          — apply user-configured filter pipeline
  5. Starring           — batch-star with human-like behaviour
  6. Persistence        — write results to SQLite
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timezone

from gh_autostar.antiban import (
    AntiBanConfig,
    StarRateLimiter,
    burst_cooldown_sleep,
    is_human_hour,
    is_weekend_slowdown_active,
    jitter_sleep,
    session_fatigue_multiplier,
    session_user_agent,
    simulate_repo_browse,
    sleep_until_human_hour,
    think_time_sleep,
)
from gh_autostar.config import Settings
from gh_autostar.core.client import GitHubAPIError, GitHubClient
from gh_autostar.core.discovery import build_strategies
from gh_autostar.core.filters import build_filters, filter_pipeline
from gh_autostar.logging_setup import get_logger
from gh_autostar.models import (
    BatchResult,
    DiscoverySource,
    Repository,
    StarRecord,
    StarStatus,
    parse_repo_slug,
)
from gh_autostar.storage.database import Database

logger = get_logger("engine")


class AutoStarEngine:
    """High-level orchestrator for a single auto-star run."""

    def __init__(
        self,
        settings: Settings,
        client: GitHubClient,
        db: Database,
    ) -> None:
        self._cfg = settings
        self._client = client
        self._db = db
        self._abc = AntiBanConfig(settings)
        self._rate_limiter = StarRateLimiter(
            db=db,
            daily_cap=self._abc.daily_star_cap,
            hourly_cap=self._abc.hourly_star_cap,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def run_batch(self, dry_run: bool = False) -> BatchResult:
        """
        Execute one complete batch: anti-ban checks → discover → filter → star → persist.
        """
        result = BatchResult()
        logger.info("=== AutoStar batch starting (dry_run=%s) ===", dry_run)
        logger.info(
            "Anti-ban: daily=%d/%d stars today, hourly=%d/%d this hour",
            self._rate_limiter.stars_today,
            self._abc.daily_star_cap,
            self._rate_limiter.stars_this_hour,
            self._abc.hourly_star_cap,
        )

        # ── Anti-ban: human hours check ───────────────────────────────────────
        if self._abc.respect_human_hours and not dry_run:
            if not is_human_hour(
                self._abc.active_hour_start,
                self._abc.active_hour_end,
                self._abc.timezone_offset,
            ):
                logger.info(
                    "Outside active hours (%d:00-%d:00 UTC+%d). Skipping batch.",
                    self._abc.active_hour_start,
                    self._abc.active_hour_end,
                    self._abc.timezone_offset,
                )
                result.finished_at = datetime.now(tz=timezone.utc)
                return result

        # ── Anti-ban: daily cap check ─────────────────────────────────────────
        if not dry_run and not self._rate_limiter.can_star():
            logger.info("Rate limiter blocked batch start.")
            result.finished_at = datetime.now(tz=timezone.utc)
            return result

        # ── Pre-load already-starred repos ────────────────────────────────────
        starred_repos = self._get_starred_set()
        logger.info("Loaded %d already-starred repos.", len(starred_repos))

        seen_repos = set(self._db.get_processed_repo_names())

        # ── Discover candidates ───────────────────────────────────────────────
        candidates = list(self._discover_all(starred_repos, seen_repos, result))
        result.total_discovered = len(candidates)
        logger.info("Discovered %d candidate repos after filtering.", len(candidates))

        if not candidates:
            logger.info("No candidates to star. Batch done.")
            result.finished_at = datetime.now(tz=timezone.utc)
            return result

        # ── Star batch ────────────────────────────────────────────────────────
        self._star_batch(
            candidates[: self._cfg.batch_size],
            starred_repos,
            result,
            dry_run=dry_run,
        )

        result.finished_at = datetime.now(tz=timezone.utc)
        if self._client.rate_limit:
            result.api_calls_remaining = self._client.rate_limit.remaining

        if not dry_run:
            self._db.save_batch_result(result)

        logger.info(
            "=== Batch complete: starred=%d skipped=%d failed=%d "
            "already_starred=%d filtered_out=%d duration=%.1fs ===",
            result.total_starred,
            result.total_skipped,
            result.total_failed,
            result.total_already_starred,
            result.total_filtered_out,
            result.duration_seconds or 0,
        )
        return result

    def star_single(self, owner: str, repo: str, dry_run: bool = False) -> StarRecord:
        """Star a specific repository by owner/repo."""
        full_name = f"{owner}/{repo}"
        record = StarRecord(
            repo_full_name=full_name,
            repo_id=0,
            status=StarStatus.PENDING,
            source=DiscoverySource.MANUAL_LIST,
        )
        try:
            if self._client.is_starred(owner, repo):
                record.status = StarStatus.ALREADY_STARRED
                return record
            if not dry_run:
                simulate_repo_browse(
                    self._client, owner, repo,
                    probability=self._abc.pre_star_browse_prob,
                )
                self._client.star_repo(owner, repo)
                self._rate_limiter.record_star()
            record.status = StarStatus.STARRED
            logger.info("%s ✓ starred%s", full_name, " (dry-run)" if dry_run else "")
        except GitHubAPIError as exc:
            record.status = StarStatus.FAILED
            record.error_message = str(exc)
            logger.error("Failed to star %s: %s", full_name, exc)
        return record

    def unstar_single(self, owner: str, repo: str) -> None:
        self._client.unstar_repo(owner, repo)
        self._db.delete_star_record(f"{owner}/{repo}")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_starred_set(self) -> set[str]:
        cached = self._db.get_cached_starred_names(ttl_hours=self._cfg.cache_ttl_hours)
        if cached is not None:
            logger.debug("Using cached starred-repos list (%d repos).", len(cached))
            return cached
        logger.info("Fetching starred-repos list from GitHub API…")
        names = self._client.get_starred_repo_names()
        self._db.cache_starred_names(names)
        return names

    def _discover_all(
        self,
        starred_repos: set[str],
        seen_repos: set[str],
        result: BatchResult,
    ) -> Iterator[Repository]:
        strategies = build_strategies(
            sources=self._cfg.sources,
            languages=self._cfg.languages,
            topic_search_terms=self._cfg.topic_search_terms,
            manual_repos=self._cfg.manual_repos,
        )
        filters = build_filters(
            min_stars=self._cfg.min_stars,
            max_stars=self._cfg.max_stars,
            min_forks=self._cfg.min_forks,
            languages=self._cfg.languages,
            exclude_forks=self._cfg.exclude_forks,
            exclude_archived=self._cfg.exclude_archived,
            exclude_private=self._cfg.exclude_private,
            require_topics=self._cfg.require_topics,
            any_topics=self._cfg.any_topics,
            exclude_owners=self._cfg.exclude_owners,
            starred_repos=starred_repos,
            seen_repos=seen_repos,
        )
        seen_this_run: set[str] = set()

        for strategy in strategies:
            logger.info("Running discovery strategy: %s", strategy.source.value)
            try:
                for repo in filter_pipeline(strategy.discover(self._client), filters):
                    if repo.full_name in seen_this_run:
                        continue
                    seen_this_run.add(repo.full_name)
                    self._db.cache_repo(repo, ttl_hours=self._cfg.cache_ttl_hours)
                    yield repo
            except Exception as exc:
                logger.error(
                    "Discovery strategy %s failed: %s", strategy.source.value, exc
                )

    def _star_batch(
        self,
        candidates: list[Repository],
        starred_repos: set[str],
        result: BatchResult,
        dry_run: bool,
    ) -> None:
        stars_this_session = 0

        for i, repo in enumerate(candidates, start=1):
            # ── Anti-ban: weekend slowdown ────────────────────────────────────
            if self._abc.weekend_slowdown and not dry_run:
                if is_weekend_slowdown_active(self._abc.weekend_factor):
                    logger.debug("Weekend slowdown: skipping %s", repo.full_name)
                    result.total_skipped += 1
                    continue

            # ── Anti-ban: hourly cap check mid-batch ──────────────────────────
            if not dry_run and not self._rate_limiter.can_star():
                if self._rate_limiter.stars_this_hour >= self._abc.hourly_star_cap:
                    self._rate_limiter.wait_for_hourly_reset()
                else:
                    logger.info("Daily cap reached mid-batch. Stopping.")
                    break

            logger.info(
                "[%d/%d] Processing %s (⭐%d) | today=%d/%d this_hour=%d/%d",
                i, len(candidates),
                repo.full_name,
                repo.stargazers_count,
                self._rate_limiter.stars_today,
                self._abc.daily_star_cap,
                self._rate_limiter.stars_this_hour,
                self._abc.hourly_star_cap,
            )

            record = StarRecord(
                repo_full_name=repo.full_name,
                repo_id=repo.id,
                status=StarStatus.PENDING,
                source=DiscoverySource.TRENDING,
            )

            if repo.full_name in starred_repos:
                record.status = StarStatus.ALREADY_STARRED
                result.add_record(record)
                continue

            # ── Anti-ban: pre-star browse simulation ──────────────────────────
            if not dry_run:
                simulate_repo_browse(
                    self._client,
                    repo.owner,
                    repo.name,
                    probability=self._abc.pre_star_browse_prob,
                )

            try:
                if dry_run:
                    record.status = StarStatus.STARRED
                    logger.info("  DRY-RUN: would star %s", repo.full_name)
                else:
                    self._client.star_repo(repo.owner, repo.name)
                    record.status = StarStatus.STARRED
                    starred_repos.add(repo.full_name)
                    self._rate_limiter.record_star()
                    stars_this_session += 1
                    self._db.invalidate_starred_cache()

            except GitHubAPIError as exc:
                record.status = StarStatus.FAILED
                record.error_message = str(exc)
                logger.error("  FAILED to star %s: %s", repo.full_name, exc)
            except Exception as exc:
                record.status = StarStatus.FAILED
                record.error_message = str(exc)
                logger.exception("  Unexpected error starring %s", repo.full_name)

            result.add_record(record)
            result.api_calls_used += 1

            if i < len(candidates) and not dry_run:
                # ── Anti-ban: burst cooldown every N stars ────────────────────
                burst_cooldown_sleep(
                    stars_in_session=stars_this_session,
                    burst_every=self._abc.burst_every,
                    cooldown_min=self._abc.burst_cooldown_min,
                    cooldown_max=self._abc.burst_cooldown_max,
                )

                # ── Anti-ban: think time (reading pause) ──────────────────────
                think_time_sleep(probability=self._abc.think_time_prob)

                # ── Anti-ban: jitter delay with session fatigue ───────────────
                fatigue = session_fatigue_multiplier(stars_this_session)
                actual_delay = jitter_sleep(
                    base_seconds=self._cfg.batch_delay_seconds * fatigue,
                    jitter_factor=self._abc.jitter_factor,
                    min_seconds=0.5,
                )
                logger.debug(
                    "  Delay: %.2fs (base=%.2f fatigue=%.2f)",
                    actual_delay,
                    self._cfg.batch_delay_seconds,
                    fatigue,
                )

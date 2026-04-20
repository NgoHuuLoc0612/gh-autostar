"""
SQLite persistence layer for gh-autostar.

Tables:
  star_records         — permanent log of all star/skip/fail events
  batch_runs           — summary of each batch execution
  repo_cache           — discovered-repo cache with TTL
  starred_names_cache  — snapshot of user's current starred repos
  settings_kv          — arbitrary key-value settings store
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

from gh_autostar.logging_setup import get_logger
from gh_autostar.models import BatchResult, Repository, StarRecord, StarStatus

logger = get_logger("database")

_SCHEMA_VERSION = 3


class Database:
    """Thread-safe SQLite wrapper with WAL mode and full schema management."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Connection management ─────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            conn = sqlite3.connect(
                self._path,
                isolation_level=None,  # autocommit; we control transactions manually
                check_same_thread=False,
                detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            try:
                yield conn
            finally:
                conn.close()

    # ── Schema management ─────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS star_records (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo_full_name  TEXT NOT NULL,
                    repo_id         INTEGER NOT NULL DEFAULT 0,
                    status          TEXT NOT NULL,
                    source          TEXT NOT NULL,
                    starred_at      TEXT NOT NULL,
                    error_message   TEXT,
                    attempt_count   INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_star_records_repo
                    ON star_records (repo_full_name);
                CREATE INDEX IF NOT EXISTS idx_star_records_status
                    ON star_records (status);

                CREATE TABLE IF NOT EXISTS batch_runs (

                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at           TEXT NOT NULL,
                    finished_at          TEXT,
                    total_discovered     INTEGER NOT NULL DEFAULT 0,
                    total_starred        INTEGER NOT NULL DEFAULT 0,
                    total_skipped        INTEGER NOT NULL DEFAULT 0,
                    total_failed         INTEGER NOT NULL DEFAULT 0,
                    total_already_starred INTEGER NOT NULL DEFAULT 0,
                    total_filtered_out   INTEGER NOT NULL DEFAULT 0,
                    api_calls_used       INTEGER NOT NULL DEFAULT 0,
                    api_calls_remaining  INTEGER
                );

                CREATE TABLE IF NOT EXISTS repo_cache (
                    full_name       TEXT PRIMARY KEY,
                    data_json       TEXT NOT NULL,
                    cached_at       TEXT NOT NULL,
                    expires_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_repo_cache_expires
                    ON repo_cache (expires_at);

                CREATE TABLE IF NOT EXISTS starred_names_cache (
                    id              INTEGER PRIMARY KEY CHECK (id = 1),
                    names_json      TEXT NOT NULL,
                    cached_at       TEXT NOT NULL,
                    expires_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings_kv (
                    key     TEXT PRIMARY KEY,
                    value   TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        current = row["version"] if row else 0

        if current < _SCHEMA_VERSION:
            logger.debug("Migrating schema from v%d to v%d.", current, _SCHEMA_VERSION)
            # Use executescript (auto-commits) so it works with isolation_level=None
            conn.executescript(
                f"DELETE FROM schema_version; "
                f"INSERT INTO schema_version (version) VALUES ({_SCHEMA_VERSION});"
            )

    # ── Star records ──────────────────────────────────────────────────────────

    def save_star_record(self, record: StarRecord) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO star_records
                    (repo_full_name, repo_id, status, source, starred_at, error_message, attempt_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.repo_full_name,
                    record.repo_id,
                    record.status.value,
                    record.source.value,
                    record.starred_at.isoformat(),
                    record.error_message,
                    record.attempt_count,
                ),
            )
            conn.execute("COMMIT")

    def delete_star_record(self, repo_full_name: str) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM star_records WHERE repo_full_name = ?", (repo_full_name,)
            )
            conn.execute("COMMIT")

    def get_star_records(
        self,
        status: StarStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[StarRecord]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM star_records
                    WHERE status = ?
                    ORDER BY starred_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (status.value, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM star_records
                    ORDER BY starred_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        return [self._row_to_star_record(r) for r in rows]

    def get_processed_repo_names(self) -> set[str]:
        """Return repos we have already successfully starred."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT repo_full_name FROM star_records WHERE status = ?",
                (StarStatus.STARRED.value,),
            ).fetchall()
        return {r["repo_full_name"] for r in rows}

    def get_star_stats(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM star_records GROUP BY status"
            ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    @staticmethod
    def _row_to_star_record(row: sqlite3.Row) -> StarRecord:
        return StarRecord(
            repo_full_name=row["repo_full_name"],
            repo_id=row["repo_id"],
            status=StarStatus(row["status"]),
            source=row["source"],
            starred_at=datetime.fromisoformat(row["starred_at"]),
            error_message=row["error_message"],
            attempt_count=row["attempt_count"],
        )

    # ── Batch runs ────────────────────────────────────────────────────────────

    def save_batch_result(self, result: BatchResult) -> int:
        """Persist a BatchResult and all its records. Returns the run ID."""
        with self._conn() as conn:
            conn.execute("BEGIN")
            cur = conn.execute(
                """
                INSERT INTO batch_runs
                    (started_at, finished_at, total_discovered, total_starred,
                     total_skipped, total_failed, total_already_starred,
                     total_filtered_out, api_calls_used, api_calls_remaining)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.started_at.isoformat(),
                    result.finished_at.isoformat() if result.finished_at else None,
                    result.total_discovered,
                    result.total_starred,
                    result.total_skipped,
                    result.total_failed,
                    result.total_already_starred,
                    result.total_filtered_out,
                    result.api_calls_used,
                    result.api_calls_remaining,
                ),
            )
            run_id = cur.lastrowid

            for record in result.records:
                conn.execute(
                    """
                    INSERT INTO star_records
                        (repo_full_name, repo_id, status, source, starred_at,
                         error_message, attempt_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.repo_full_name,
                        record.repo_id,
                        record.status.value,
                        record.source.value,
                        record.starred_at.isoformat(),
                        record.error_message,
                        record.attempt_count,
                    ),
                )

            conn.execute("COMMIT")
        return run_id or 0

    def get_batch_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM batch_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Repo cache ────────────────────────────────────────────────────────────

    def cache_repo(self, repo: Repository, ttl_hours: int = 6) -> None:
        now = datetime.now(tz=timezone.utc)
        expires = now + timedelta(hours=ttl_hours)
        with self._conn() as conn:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO repo_cache (full_name, data_json, cached_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(full_name) DO UPDATE SET
                    data_json  = excluded.data_json,
                    cached_at  = excluded.cached_at,
                    expires_at = excluded.expires_at
                """,
                (
                    repo.full_name,
                    repo.model_dump_json(),
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            conn.execute("COMMIT")

    def get_cached_repo(self, full_name: str) -> Repository | None:
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data_json FROM repo_cache WHERE full_name = ? AND expires_at > ?",
                (full_name, now),
            ).fetchone()
        if not row:
            return None
        return Repository.model_validate_json(row["data_json"])

    def prune_expired_cache(self) -> int:
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN")
            cur = conn.execute("DELETE FROM repo_cache WHERE expires_at <= ?", (now,))
            conn.execute("COMMIT")
        return cur.rowcount

    def get_cached_repos(self) -> list[Repository]:
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data_json FROM repo_cache WHERE expires_at > ? ORDER BY cached_at DESC",
                (now,),
            ).fetchall()
        repos = []
        for row in rows:
            try:
                repos.append(Repository.model_validate_json(row["data_json"]))
            except Exception:
                pass
        return repos

    # ── Starred names cache ───────────────────────────────────────────────────

    def cache_starred_names(self, names: set[str], ttl_hours: int = 6) -> None:
        now = datetime.now(tz=timezone.utc)
        expires = now + timedelta(hours=ttl_hours)
        with self._conn() as conn:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO starred_names_cache (id, names_json, cached_at, expires_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    names_json = excluded.names_json,
                    cached_at  = excluded.cached_at,
                    expires_at = excluded.expires_at
                """,
                (json.dumps(sorted(names)), now.isoformat(), expires.isoformat()),
            )
            conn.execute("COMMIT")

    def get_cached_starred_names(self, ttl_hours: int = 6) -> set[str] | None:
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT names_json FROM starred_names_cache WHERE id = 1 AND expires_at > ?",
                (now,),
            ).fetchone()
        if not row:
            return None
        return set(json.loads(row["names_json"]))

    def invalidate_starred_cache(self) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM starred_names_cache WHERE id = 1")
            conn.execute("COMMIT")

    # ── Settings KV ──────────────────────────────────────────────────────────

    def set_setting(self, key: str, value: str) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO settings_kv (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )
            conn.execute("COMMIT")

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM settings_kv WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    # ── Maintenance ───────────────────────────────────────────────────────────

    def vacuum(self) -> None:
        with self._conn() as conn:
            conn.execute("VACUUM")

    def get_db_stats(self) -> dict[str, int]:
        with self._conn() as conn:
            return {
                "star_records": conn.execute("SELECT COUNT(*) FROM star_records").fetchone()[0],
                "batch_runs": conn.execute("SELECT COUNT(*) FROM batch_runs").fetchone()[0],
                "repo_cache": conn.execute("SELECT COUNT(*) FROM repo_cache").fetchone()[0],
            }

    # ── Analytics queries ─────────────────────────────────────────────────────

    def get_stars_per_day(self, days: int = 90) -> list[dict]:
        """Return [{date, count}] for the last N days."""
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT substr(starred_at, 1, 10) AS date, COUNT(*) AS count
                FROM star_records
                WHERE status = 'starred' AND starred_at >= ?
                GROUP BY date
                ORDER BY date
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stars_per_week(self, weeks: int = 26) -> list[dict]:
        """Return [{week, count}] for the last N weeks."""
        cutoff = (datetime.now(tz=timezone.utc) - timedelta(weeks=weeks)).isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT strftime('%Y-W%W', starred_at) AS week, COUNT(*) AS count
                FROM star_records
                WHERE status = 'starred' AND starred_at >= ?
                GROUP BY week
                ORDER BY week
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stars_per_hour_of_day(self) -> list[dict]:
        """Return [{hour, count}] — activity heatmap by hour."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT CAST(substr(starred_at, 12, 2) AS INTEGER) AS hour,
                       COUNT(*) AS count
                FROM star_records
                WHERE status = 'starred'
                GROUP BY hour
                ORDER BY hour
                """,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_language_breakdown(self) -> list[dict]:
        """Return [{language, count}] from repo_cache for starred repos."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    json_extract(rc.data_json, '$.language') AS language,
                    COUNT(*) AS count
                FROM star_records sr
                LEFT JOIN repo_cache rc ON rc.full_name = sr.repo_full_name
                WHERE sr.status = 'starred'
                GROUP BY language
                ORDER BY count DESC
                """,
            ).fetchall()
        return [
            {"language": r["language"] or "Unknown", "count": r["count"]}
            for r in rows
        ]

    def get_source_breakdown(self) -> list[dict]:
        """Return [{source, count}] — which strategy discovered the most repos."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT source, COUNT(*) AS count
                FROM star_records
                WHERE status = 'starred'
                GROUP BY source
                ORDER BY count DESC
                """,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stars_per_day_of_week(self) -> list[dict]:
        """Return [{dow, label, count}] 0=Sunday … 6=Saturday."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT CAST(strftime('%w', starred_at) AS INTEGER) AS dow,
                       COUNT(*) AS count
                FROM star_records
                WHERE status = 'starred'
                GROUP BY dow
                ORDER BY dow
                """,
            ).fetchall()
        labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        return [{"dow": r["dow"], "label": labels[r["dow"]], "count": r["count"]} for r in rows]

    def get_top_starred_repos(self, limit: int = 20) -> list[dict]:
        """Return top repos by star count (from cache)."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    sr.repo_full_name,
                    sr.starred_at,
                    sr.source,
                    COALESCE(json_extract(rc.data_json, '$.stargazers_count'), 0) AS stars,
                    COALESCE(json_extract(rc.data_json, '$.language'), 'Unknown') AS language,
                    COALESCE(json_extract(rc.data_json, '$.description'), '') AS description
                FROM star_records sr
                LEFT JOIN repo_cache rc ON rc.full_name = sr.repo_full_name
                WHERE sr.status = 'starred'
                ORDER BY stars DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_batch_performance(self, limit: int = 30) -> list[dict]:
        """Return batch run details for performance charting."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    id, started_at, finished_at,
                    total_starred, total_failed, total_filtered_out,
                    total_already_starred, api_calls_used
                FROM batch_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_cumulative_stars(self) -> list[dict]:
        """Return [{date, cumulative}] for growth curve chart."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT substr(starred_at, 1, 10) AS date, COUNT(*) AS daily
                FROM star_records
                WHERE status = 'starred'
                GROUP BY date
                ORDER BY date
                """,
            ).fetchall()
        cumulative = 0
        result = []
        for r in rows:
            cumulative += r["daily"]
            result.append({"date": r["date"], "cumulative": cumulative, "daily": r["daily"]})
        return result

    def get_full_stats_summary(self) -> dict:
        """Single-query stats for email digest / summary card."""
        with self._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM star_records WHERE status='starred'"
            ).fetchone()[0]
            this_week = conn.execute(
                """
                SELECT COUNT(*) FROM star_records
                WHERE status='starred'
                  AND starred_at >= datetime('now', '-7 days')
                """
            ).fetchone()[0]
            today = conn.execute(
                """
                SELECT COUNT(*) FROM star_records
                WHERE status='starred'
                  AND substr(starred_at,1,10) = date('now')
                """
            ).fetchone()[0]
            total_failed = conn.execute(
                "SELECT COUNT(*) FROM star_records WHERE status='failed'"
            ).fetchone()[0]
            total_batches = conn.execute("SELECT COUNT(*) FROM batch_runs").fetchone()[0]
        return {
            "total_starred": total,
            "starred_this_week": this_week,
            "starred_today": today,
            "total_failed": total_failed,
            "total_batches": total_batches,
        }


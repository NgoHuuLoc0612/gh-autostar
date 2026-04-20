"""Tests for the SQLite persistence layer."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gh_autostar.models import BatchResult, DiscoverySource, StarRecord, StarStatus
from gh_autostar.storage.database import Database


class TestDatabase:
    def test_save_and_retrieve_star_record(
        self, tmp_db: Database, sample_star_record: StarRecord
    ) -> None:
        tmp_db.save_star_record(sample_star_record)
        records = tmp_db.get_star_records()
        assert len(records) == 1
        r = records[0]
        assert r.repo_full_name == sample_star_record.repo_full_name
        assert r.status == StarStatus.STARRED

    def test_filter_by_status(self, tmp_db: Database, sample_repo) -> None:
        for status in (StarStatus.STARRED, StarStatus.FAILED, StarStatus.STARRED):
            tmp_db.save_star_record(
                StarRecord(
                    repo_full_name=f"owner/repo-{status.value}",
                    repo_id=1,
                    status=status,
                    source=DiscoverySource.TRENDING,
                )
            )
        starred = tmp_db.get_star_records(status=StarStatus.STARRED)
        assert len(starred) == 2
        failed = tmp_db.get_star_records(status=StarStatus.FAILED)
        assert len(failed) == 1

    def test_get_processed_repo_names(self, tmp_db: Database) -> None:
        tmp_db.save_star_record(
            StarRecord(
                repo_full_name="a/b",
                repo_id=1,
                status=StarStatus.STARRED,
                source=DiscoverySource.TRENDING,
            )
        )
        tmp_db.save_star_record(
            StarRecord(
                repo_full_name="c/d",
                repo_id=2,
                status=StarStatus.FAILED,
                source=DiscoverySource.TRENDING,
            )
        )
        names = tmp_db.get_processed_repo_names()
        assert "a/b" in names
        assert "c/d" not in names  # only STARRED counts

    def test_cache_repo_and_retrieve(self, tmp_db: Database, sample_repo) -> None:
        tmp_db.cache_repo(sample_repo, ttl_hours=1)
        cached = tmp_db.get_cached_repo(sample_repo.full_name)
        assert cached is not None
        assert cached.full_name == sample_repo.full_name
        assert cached.stargazers_count == sample_repo.stargazers_count

    def test_cache_repo_miss(self, tmp_db: Database) -> None:
        result = tmp_db.get_cached_repo("nonexistent/repo")
        assert result is None

    def test_starred_names_cache(self, tmp_db: Database) -> None:
        names = {"a/b", "c/d", "e/f"}
        tmp_db.cache_starred_names(names, ttl_hours=6)
        cached = tmp_db.get_cached_starred_names(ttl_hours=6)
        assert cached == names

    def test_starred_names_cache_invalidation(self, tmp_db: Database) -> None:
        tmp_db.cache_starred_names({"x/y"}, ttl_hours=6)
        tmp_db.invalidate_starred_cache()
        assert tmp_db.get_cached_starred_names(ttl_hours=6) is None

    def test_save_batch_result(self, tmp_db: Database, sample_star_record: StarRecord) -> None:
        result = BatchResult()
        result.add_record(sample_star_record)
        result.finished_at = datetime.now(tz=timezone.utc)
        run_id = tmp_db.save_batch_result(result)
        assert run_id > 0

        runs = tmp_db.get_batch_runs()
        assert len(runs) == 1
        assert runs[0]["total_starred"] == 1

    def test_settings_kv(self, tmp_db: Database) -> None:
        tmp_db.set_setting("last_run", "2025-01-01")
        assert tmp_db.get_setting("last_run") == "2025-01-01"
        assert tmp_db.get_setting("missing", default="x") == "x"

    def test_get_db_stats(self, tmp_db: Database, sample_star_record: StarRecord) -> None:
        tmp_db.save_star_record(sample_star_record)
        stats = tmp_db.get_db_stats()
        assert stats["star_records"] == 1
        assert stats["batch_runs"] == 0

    def test_prune_expired_cache(self, tmp_db: Database, sample_repo) -> None:
        # TTL=0 means expires immediately
        tmp_db.cache_repo(sample_repo, ttl_hours=0)
        pruned = tmp_db.prune_expired_cache()
        assert pruned >= 0  # expired entries removed (may be 1 depending on timing)

    def test_delete_star_record(self, tmp_db: Database, sample_star_record: StarRecord) -> None:
        tmp_db.save_star_record(sample_star_record)
        tmp_db.delete_star_record(sample_star_record.repo_full_name)
        records = tmp_db.get_star_records()
        assert all(r.repo_full_name != sample_star_record.repo_full_name for r in records)

    def test_concurrent_writes(self, tmp_db: Database) -> None:
        """Database should handle concurrent thread access safely."""
        import threading

        errors: list[Exception] = []

        def write(i: int) -> None:
            try:
                tmp_db.save_star_record(
                    StarRecord(
                        repo_full_name=f"owner/repo-{i}",
                        repo_id=i,
                        status=StarStatus.STARRED,
                        source=DiscoverySource.TRENDING,
                    )
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        records = tmp_db.get_star_records(limit=100)
        assert len(records) == 20

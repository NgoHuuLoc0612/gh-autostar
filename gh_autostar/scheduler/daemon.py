"""
Background daemon powered by APScheduler.

Runs the AutoStar engine on a configurable interval.
Supports graceful shutdown via SIGTERM / SIGINT.
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime, timezone
from typing import NoReturn

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from gh_autostar.config import Settings, get_settings
from gh_autostar.core.client import GitHubClient
from gh_autostar.core.engine import AutoStarEngine
from gh_autostar.logging_setup import get_logger, setup_logging
from gh_autostar.storage.database import Database

logger = get_logger("daemon")


class AutoStarDaemon:
    """Long-running process that periodically triggers the engine."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = settings or get_settings()
        self._scheduler: BackgroundScheduler | None = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, foreground: bool = True) -> NoReturn | None:
        """Start the daemon. Blocks if foreground=True."""
        setup_logging(
            level=self._cfg.log_level,
            log_file=self._cfg.log_file,
            max_bytes=self._cfg.log_max_bytes,
            backup_count=self._cfg.log_backup_count,
        )

        logger.info(
            "gh-autostar daemon starting (interval=%dm, sources=%s).",
            self._cfg.scheduler_interval_minutes,
            self._cfg.sources,
        )

        self._setup_signal_handlers()
        self._scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 120},
            timezone="UTC",
        )

        # Schedule the periodic job
        self._scheduler.add_job(
            self._run_job,
            trigger=IntervalTrigger(minutes=self._cfg.scheduler_interval_minutes),
            id="autostar_batch",
            name="AutoStar Batch",
            next_run_time=(
                datetime.now(tz=timezone.utc)
                if not self._cfg.startup_delay_seconds
                else None
            ),
        )

        self._scheduler.start()
        self._running = True

        # Startup delay before first run
        if self._cfg.startup_delay_seconds:
            logger.info(
                "Waiting %.0fs before first run…", self._cfg.startup_delay_seconds
            )
            time.sleep(self._cfg.startup_delay_seconds)
            self._run_job()

        if foreground:
            self._block()

        return None

    def stop(self) -> None:
        logger.info("Daemon shutting down…")
        self._running = False
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        logger.info("Daemon stopped.")

    def trigger_now(self) -> None:
        """Manually fire one batch run immediately (for CLI `run` command)."""
        self._run_job()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_job(self) -> None:
        logger.info("Scheduled job fired at %s UTC.", datetime.now(tz=timezone.utc).isoformat())
        cfg = get_settings(reload=True)
        db = Database(cfg.database_path)

        try:
            with GitHubClient(
                token=cfg.github_token,
                base_url=cfg.github_api_base,
                timeout=cfg.http_timeout_seconds,
                max_retries=cfg.http_max_retries,
                backoff_factor=cfg.http_backoff_factor,
                rate_limit_buffer=cfg.rate_limit_buffer,
            ) as client:
                engine = AutoStarEngine(settings=cfg, client=client, db=db)
                result = engine.run_batch()

            if cfg.notify_on_completion:
                _send_notification(
                    f"gh-autostar: starred {result.total_starred} repos "
                    f"in {result.duration_seconds:.0f}s"
                )
        except Exception as exc:
            logger.exception("Job failed: %s", exc)

    def _block(self) -> NoReturn:
        try:
            while self._running:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            self.stop()
        sys.exit(0)

    def _setup_signal_handlers(self) -> None:
        def _handler(signum: int, frame: object) -> None:
            logger.info("Received signal %d, shutting down.", signum)
            self.stop()
            sys.exit(0)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass  # can't set signals in threads


def _send_notification(message: str) -> None:
    """Best-effort desktop notification."""
    import platform
    import subprocess
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript", "-e", f'display notification "{message}" with title "gh-autostar"'],
                check=False,
                timeout=5,
            )
        elif system == "Linux":
            subprocess.run(
                ["notify-send", "gh-autostar", message],
                check=False,
                timeout=5,
            )
        # Windows: could use win10toast or winreg; skipped for brevity
    except Exception:
        pass

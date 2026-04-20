"""
gh-autostar — Automated GitHub repository starring.

Batch-star repos, run on startup, cache everything in SQLite.
"""

from gh_autostar._version import __version__
from gh_autostar.core.client import GitHubClient
from gh_autostar.core.engine import AutoStarEngine
from gh_autostar.storage.database import Database
from gh_autostar.scheduler.daemon import AutoStarDaemon

__all__ = [
    "__version__",
    "GitHubClient",
    "AutoStarEngine",
    "Database",
    "AutoStarDaemon",
]

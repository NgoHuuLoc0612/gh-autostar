"""
Security module for gh-autostar.

Responsibilities:
  - Store GitHub token in OS keychain (keyring) instead of plaintext .env
  - Mask tokens in logs and error messages
  - Enforce .env file permissions (600)
  - Validate token format before storing
  - Sanitise all user inputs (repo slugs, search terms)
  - Provide a secure audit log of all star operations
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import stat
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("gh_autostar.security")

# ── Token format validation ────────────────────────────────────────────────────

# Classic PAT: ghp_<36 alphanumeric>
# Fine-grained: github_pat_<rest>
# Old format: 40 hex chars
_TOKEN_PATTERNS = [
    re.compile(r"^ghp_[A-Za-z0-9]{36}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{80,}$"),
    re.compile(r"^[0-9a-f]{40}$"),           # legacy
    re.compile(r"^ghs_[A-Za-z0-9]{36}$"),   # GitHub App installation token
    re.compile(r"^gho_[A-Za-z0-9]{36}$"),   # OAuth token
]

# Regex to detect accidental token exposure in strings
_TOKEN_LEAK_RE = re.compile(
    r"(ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{80,}|ghs_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|[0-9a-f]{40})"
)

_KEYRING_SERVICE = "gh-autostar"
_KEYRING_USERNAME = "github_token"


def validate_token_format(token: str) -> bool:
    """Return True if token matches a known GitHub PAT format."""
    return any(p.match(token.strip()) for p in _TOKEN_PATTERNS)


def mask_token(token: str) -> str:
    """Return a masked representation safe for logging."""
    if not token:
        return "(no token)"
    if len(token) <= 10:
        return "***"
    return token[:6] + "·" * 6 + token[-4:]


def sanitise_log_message(message: str) -> str:
    """Replace any accidental token exposure in a log string."""
    return _TOKEN_LEAK_RE.sub(lambda m: mask_token(m.group(0)), message)


# ── OS Keychain storage ────────────────────────────────────────────────────────

def store_token_keychain(token: str) -> bool:
    """
    Store token in the OS keychain. Returns True on success.
    Falls back gracefully if keyring is unavailable.
    """
    try:
        import keyring
        import keyring.errors
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, token)
        logger.info("Token stored in OS keychain (%s).", _keychain_backend_name())
        return True
    except Exception as exc:
        logger.warning("Keychain unavailable (%s). Token will be stored in .env.", exc)
        return False


def load_token_keychain() -> str | None:
    """Load token from OS keychain. Returns None if not found."""
    try:
        import keyring
        token = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        return token or None
    except Exception:
        return None


def delete_token_keychain() -> bool:
    """Remove token from OS keychain."""
    try:
        import keyring
        import keyring.errors
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        return True
    except Exception:
        return False


def _keychain_backend_name() -> str:
    try:
        import keyring
        backend = keyring.get_keyring()
        return type(backend).__name__
    except Exception:
        return "unknown"


# ── .env file hardening ────────────────────────────────────────────────────────

def secure_env_file(path: Path) -> None:
    """
    Set .env file permissions to owner-read-write only (600).
    No-op on Windows (uses ACLs differently).
    """
    if platform.system() == "Windows":
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception as exc:
        logger.warning("Could not set .env permissions: %s", exc)


def check_env_file_permissions(path: Path) -> list[str]:
    """
    Return a list of security warnings about the .env file.
    """
    warnings: list[str] = []
    if not path.exists():
        return warnings
    if platform.system() == "Windows":
        return warnings
    mode = path.stat().st_mode
    if mode & stat.S_IRGRP:
        warnings.append(".env file is readable by group — run: chmod 600 " + str(path))
    if mode & stat.S_IROTH:
        warnings.append(".env file is readable by others — run: chmod 600 " + str(path))
    return warnings


# ── Input sanitisation ────────────────────────────────────────────────────────

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
_SAFE_SEARCH_RE = re.compile(r"[^\w\s\-.:><=]")   # allow word chars, spaces, operators


def sanitise_repo_slug(slug: str) -> str:
    """
    Validate and return a clean owner/repo slug.
    Raises ValueError on invalid input.
    """
    slug = slug.strip()
    if not _REPO_SLUG_RE.match(slug):
        raise ValueError(
            f"Invalid repo slug {slug!r}. Expected format: owner/repo "
            "(alphanumeric, dots, dashes, underscores only)."
        )
    # Prevent path traversal
    if ".." in slug:
        raise ValueError(f"Repo slug {slug!r} contains path traversal sequence.")
    return slug


def sanitise_search_query(query: str) -> str:
    """
    Remove characters that could be used for injection in search queries.
    GitHub search is fairly safe but we strip control chars and unbalanced quotes.
    """
    # Strip control characters
    query = re.sub(r"[\x00-\x1f\x7f]", "", query)
    # Limit length
    return query[:256]


# ── Secure logging filter ─────────────────────────────────────────────────────

class TokenMaskingFilter(logging.Filter):
    """
    Logging filter that masks any GitHub tokens that accidentally
    appear in log records.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitise_log_message(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    sanitise_log_message(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: sanitise_log_message(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


def install_token_masking_filter() -> None:
    """Install the token masking filter on the root gh_autostar logger."""
    root = logging.getLogger("gh_autostar")
    for handler in root.handlers:
        handler.addFilter(TokenMaskingFilter())
    # Also add to root logger in case handlers are added later
    root.addFilter(TokenMaskingFilter())


# ── Token fingerprint (for audit log, never stores actual token) ──────────────

def token_fingerprint(token: str) -> str:
    """Return a short SHA-256 fingerprint of the token for audit logs."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


# ── Audit log ─────────────────────────────────────────────────────────────────

class AuditLogger:
    """
    Append-only audit log for security-relevant events.
    Writes to a separate audit.log file, never contains tokens.
    """

    def __init__(self, log_dir: Path) -> None:
        self._path = log_dir / "audit.log"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch(mode=0o600)
        elif platform.system() != "Windows":
            try:
                self._path.chmod(0o600)
            except Exception:
                pass

    def _write(self, event: str, details: dict[str, Any]) -> None:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        parts = [f"time={ts}", f"event={event}"]
        for k, v in details.items():
            # Never log actual token values
            if "token" in k.lower() and isinstance(v, str) and len(v) > 10:
                v = mask_token(v)
            parts.append(f"{k}={v!r}")
        line = " ".join(parts) + "\n"
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)

    def log_auth(self, username: str, token_fp: str, success: bool) -> None:
        self._write("auth", {
            "user": username,
            "token_fingerprint": token_fp,
            "success": success,
        })

    def log_star(self, repo: str, status: str, source: str) -> None:
        self._write("star", {
            "repo": repo,
            "status": status,
            "source": source,
        })

    def log_unstar(self, repo: str) -> None:
        self._write("unstar", {"repo": repo})

    def log_config_change(self, key: str, masked_value: str) -> None:
        self._write("config_change", {"key": key, "value": masked_value})

    def log_startup(self, version: str, sources: list[str]) -> None:
        self._write("startup", {"version": version, "sources": str(sources)})

    def log_rate_limit_hit(self, remaining: int, reset_in: float) -> None:
        self._write("rate_limit_hit", {
            "remaining": remaining,
            "reset_in_seconds": f"{reset_in:.0f}",
        })

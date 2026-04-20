"""
Anti-ban / human-behavior simulation module.

Strategies implemented:
  1. Jitter delays       — Gaussian-distributed delays, never fixed
  2. Human hours         — only active during configurable waking hours
  3. Daily star cap      — hard limit on stars/day to avoid abuse flags
  4. Hourly star cap     — burst protection within an hour
  5. Pre-star browse     — GET /repos/{o}/{r} before PUT star (human reads first)
  6. Session fatigue     — slower as session progresses (humans get tired)
  7. Cooldown bursts     — longer pause every N stars
  8. Weekend slowdown    — reduced activity on weekends
  9. Rotating User-Agent — plausible browser-like UA strings
 10. Think time          — occasional longer pause (simulates reading README)
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from gh_autostar.logging_setup import get_logger
from gh_autostar.storage.database import Database

if TYPE_CHECKING:
    from gh_autostar.config import Settings

logger = get_logger("antiban")

# ── User-Agent pool ───────────────────────────────────────────────────────────
# Realistic browser UAs that GitHub's own web UI would send.
# Rotated per session (not per request) to avoid pattern on UA switching.

_USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# Pick one UA per process lifetime (stable session)
_SESSION_UA = random.choice(_USER_AGENTS)


def session_user_agent() -> str:
    """Return this session's User-Agent string."""
    return _SESSION_UA


# ── Human hours ───────────────────────────────────────────────────────────────

def is_human_hour(
    active_start: int = 8,
    active_end: int = 23,
    timezone_offset_hours: int = 0,
) -> bool:
    """
    Return True if current local time is within human waking hours.
    active_start / active_end are 24h clock values in local time.
    """
    now_utc = datetime.now(tz=timezone.utc)
    local_hour = (now_utc.hour + timezone_offset_hours) % 24
    return active_start <= local_hour < active_end


def is_weekend_slowdown_active(slowdown_factor: float = 0.4) -> bool:
    """
    On weekends, returns True with probability (1 - slowdown_factor).
    I.e. with factor=0.4, 60% of weekend checks will say 'skip this star'.
    """
    now = datetime.now(tz=timezone.utc)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return random.random() > slowdown_factor
    return False


def sleep_until_human_hour(
    active_start: int = 8,
    active_end: int = 23,
    timezone_offset_hours: int = 0,
    check_interval: int = 300,
) -> None:
    """Block until the current time is within human hours."""
    while not is_human_hour(active_start, active_end, timezone_offset_hours):
        now_utc = datetime.now(tz=timezone.utc)
        local_hour = (now_utc.hour + timezone_offset_hours) % 24
        logger.info(
            "Outside active hours (local hour=%d, active=%d-%d). "
            "Sleeping %ds.",
            local_hour, active_start, active_end, check_interval,
        )
        time.sleep(check_interval)


# ── Jitter delays ─────────────────────────────────────────────────────────────

def jitter_sleep(
    base_seconds: float,
    jitter_factor: float = 0.5,
    min_seconds: float = 0.5,
) -> float:
    """
    Sleep for a human-like randomised duration.

    Uses a log-normal distribution centred around base_seconds.
    Log-normal is appropriate because:
      - Always positive
      - Right-skewed (occasional longer pauses like humans)
      - Mean ≈ base_seconds when sigma is small

    Returns actual sleep duration.
    """
    # log-normal: mu and sigma in log-space
    sigma = jitter_factor * 0.6
    mu = math.log(max(base_seconds, 0.1))
    duration = random.lognormvariate(mu, sigma)
    duration = max(duration, min_seconds)
    time.sleep(duration)
    return duration


def think_time_sleep(
    min_seconds: float = 3.0,
    max_seconds: float = 12.0,
    probability: float = 0.15,
) -> bool:
    """
    With given probability, sleep for a longer 'think time'
    (simulates user reading repo description / README).
    Returns True if think-time sleep occurred.
    """
    if random.random() < probability:
        duration = random.uniform(min_seconds, max_seconds)
        logger.debug("Think-time pause: %.1fs", duration)
        time.sleep(duration)
        return True
    return False


def burst_cooldown_sleep(
    stars_in_session: int,
    burst_every: int = 10,
    cooldown_min: float = 15.0,
    cooldown_max: float = 45.0,
) -> bool:
    """
    Every `burst_every` stars, take a longer cooldown break.
    Returns True if cooldown occurred.
    """
    if stars_in_session > 0 and stars_in_session % burst_every == 0:
        duration = random.uniform(cooldown_min, cooldown_max)
        logger.info(
            "Burst cooldown after %d stars: %.0fs",
            stars_in_session, duration,
        )
        time.sleep(duration)
        return True
    return False


def session_fatigue_multiplier(stars_in_session: int, fatigue_rate: float = 0.03) -> float:
    """
    Return a slowdown multiplier that increases as the session progresses.
    Models human fatigue — people slow down as they browse longer.

    multiplier = 1.0 + fatigue_rate * sqrt(stars_in_session)
    At 0 stars: 1.0x  (normal speed)
    At 10 stars: ~1.09x
    At 30 stars: ~1.16x
    At 100 stars: ~1.30x
    """
    return 1.0 + fatigue_rate * math.sqrt(stars_in_session)


# ── Daily / hourly caps ───────────────────────────────────────────────────────

class StarRateLimiter:
    """
    Enforces daily and hourly star count caps to avoid abuse signals.
    Persists counts in the SQLite database so caps survive restarts.
    """

    _DAILY_KEY  = "antiban_stars_today"
    _HOURLY_KEY = "antiban_stars_this_hour"
    _DATE_KEY   = "antiban_date"
    _HOUR_KEY   = "antiban_hour"

    def __init__(
        self,
        db: Database,
        daily_cap: int = 150,
        hourly_cap: int = 25,
    ) -> None:
        self._db = db
        self._daily_cap = daily_cap
        self._hourly_cap = hourly_cap
        self._ensure_fresh()

    def _ensure_fresh(self) -> None:
        """Reset counters if the day or hour has rolled over."""
        now = datetime.now(tz=timezone.utc)
        today = now.strftime("%Y-%m-%d")
        this_hour = now.strftime("%Y-%m-%d-%H")

        stored_date = self._db.get_setting(self._DATE_KEY, "")
        stored_hour = self._db.get_setting(self._HOUR_KEY, "")

        if stored_date != today:
            self._db.set_setting(self._DAILY_KEY, "0")
            self._db.set_setting(self._DATE_KEY, today)

        if stored_hour != this_hour:
            self._db.set_setting(self._HOURLY_KEY, "0")
            self._db.set_setting(self._HOUR_KEY, this_hour)

    @property
    def stars_today(self) -> int:
        self._ensure_fresh()
        return int(self._db.get_setting(self._DAILY_KEY, "0") or 0)

    @property
    def stars_this_hour(self) -> int:
        self._ensure_fresh()
        return int(self._db.get_setting(self._HOURLY_KEY, "0") or 0)

    def can_star(self) -> bool:
        """Return True if we're under both daily and hourly caps."""
        self._ensure_fresh()
        if self.stars_today >= self._daily_cap:
            logger.warning(
                "Daily star cap reached (%d/%d). Stopping for today.",
                self.stars_today, self._daily_cap,
            )
            return False
        if self.stars_this_hour >= self._hourly_cap:
            logger.warning(
                "Hourly star cap reached (%d/%d). Pausing until next hour.",
                self.stars_this_hour, self._hourly_cap,
            )
            return False
        return True

    def record_star(self) -> None:
        """Increment both counters after a successful star."""
        self._ensure_fresh()
        daily  = int(self._db.get_setting(self._DAILY_KEY,  "0") or 0) + 1
        hourly = int(self._db.get_setting(self._HOURLY_KEY, "0") or 0) + 1
        self._db.set_setting(self._DAILY_KEY,  str(daily))
        self._db.set_setting(self._HOURLY_KEY, str(hourly))

    def remaining_today(self) -> int:
        return max(0, self._daily_cap - self.stars_today)

    def remaining_this_hour(self) -> int:
        return max(0, self._hourly_cap - self.stars_this_hour)

    def wait_for_hourly_reset(self) -> None:
        """Sleep until the next hour boundary."""
        now = datetime.now(tz=timezone.utc)
        seconds_until_next_hour = 3600 - (now.minute * 60 + now.second)
        wait = seconds_until_next_hour + random.randint(30, 120)  # +jitter
        logger.info("Hourly cap hit. Sleeping %ds until reset.", wait)
        time.sleep(wait)


# ── Pre-star browse simulation ────────────────────────────────────────────────

def simulate_repo_browse(
    client: "object",  # GitHubClient — avoid circular import
    owner: str,
    repo: str,
    probability: float = 0.6,
) -> None:
    """
    With given probability, fetch the repo page before starring it.
    Humans browse the repo before starring — pure starring without
    any GET is a strong bot signal.

    Also occasionally fetches README and contributors.
    """
    if random.random() > probability:
        return

    try:
        # Primary browse: GET /repos/{owner}/{repo}
        client._get(f"/repos/{owner}/{repo}")  # type: ignore[attr-defined]
        logger.debug("Browse: %s/%s", owner, repo)

        # 20% chance of also peeking at contributors
        if random.random() < 0.20:
            client._get(f"/repos/{owner}/{repo}/contributors", {"per_page": 5})  # type: ignore[attr-defined]

        # Brief pause after browsing (reading behaviour)
        time.sleep(random.uniform(0.5, 2.5))

    except Exception:
        pass  # Browse failure is non-critical


# ── Config dataclass ──────────────────────────────────────────────────────────

class AntiBanConfig:
    """
    All anti-ban parameters in one place.
    Derived from Settings but with sane standalone defaults.
    """

    def __init__(self, settings: "Settings | None" = None) -> None:
        if settings is not None:
            self.active_hour_start:     int   = settings.active_hour_start
            self.active_hour_end:       int   = settings.active_hour_end
            self.timezone_offset:       int   = settings.timezone_offset_hours
            self.daily_star_cap:        int   = settings.daily_star_cap
            self.hourly_star_cap:       int   = settings.hourly_star_cap
            self.base_delay:            float = settings.batch_delay_seconds
            self.jitter_factor:         float = settings.jitter_factor
            self.think_time_prob:       float = settings.think_time_probability
            self.burst_every:           int   = settings.burst_cooldown_every
            self.burst_cooldown_min:    float = settings.burst_cooldown_min_seconds
            self.burst_cooldown_max:    float = settings.burst_cooldown_max_seconds
            self.pre_star_browse_prob:  float = settings.pre_star_browse_probability
            self.respect_human_hours:   bool  = settings.respect_human_hours
            self.weekend_slowdown:      bool  = settings.weekend_slowdown
            self.weekend_factor:        float = settings.weekend_slowdown_factor
        else:
            self.active_hour_start     = 8
            self.active_hour_end       = 23
            self.timezone_offset       = 0
            self.daily_star_cap        = 150
            self.hourly_star_cap       = 25
            self.base_delay            = 2.0
            self.jitter_factor         = 0.5
            self.think_time_prob       = 0.15
            self.burst_every           = 10
            self.burst_cooldown_min    = 15.0
            self.burst_cooldown_max    = 45.0
            self.pre_star_browse_prob  = 0.6
            self.respect_human_hours   = True
            self.weekend_slowdown      = True
            self.weekend_factor        = 0.4

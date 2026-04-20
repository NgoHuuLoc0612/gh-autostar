"""
Weekly email digest via SMTP.

Features:
  - HTML + plain-text multipart email
  - Embedded inline stats (no external images)
  - Configurable SMTP: Gmail, Outlook, custom SMTP relay
  - App-password support (Gmail 2FA)
  - Scheduled weekly send via APScheduler
  - Jinja2-free (pure string templates — no extra dep)
"""

from __future__ import annotations

import smtplib
import ssl
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from gh_autostar.logging_setup import get_logger
from gh_autostar.storage.database import Database

logger = get_logger("digest")


# ── Config dataclass ──────────────────────────────────────────────────────────

class SmtpConfig:
    """SMTP connection parameters."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool = True,
        use_starttls: bool = False,
        from_addr: str = "",
        to_addrs: list[str] | None = None,
    ) -> None:
        self.host       = host
        self.port       = port
        self.username   = username
        self.password   = password
        self.use_tls    = use_tls        # SSL/TLS (port 465)
        self.use_starttls = use_starttls # STARTTLS (port 587)
        self.from_addr  = from_addr or username
        self.to_addrs   = to_addrs or [username]

    @classmethod
    def gmail(cls, username: str, app_password: str, to: list[str] | None = None) -> "SmtpConfig":
        """Gmail with App Password (requires 2FA enabled)."""
        return cls(
            host="smtp.gmail.com", port=465,
            username=username, password=app_password,
            use_tls=True, use_starttls=False,
            to_addrs=to or [username],
        )

    @classmethod
    def outlook(cls, username: str, password: str, to: list[str] | None = None) -> "SmtpConfig":
        """Outlook / Hotmail / Office365."""
        return cls(
            host="smtp.office365.com", port=587,
            username=username, password=password,
            use_tls=False, use_starttls=True,
            to_addrs=to or [username],
        )

    @classmethod
    def from_settings(cls, settings: "Any") -> "SmtpConfig":
        """Build from gh-autostar Settings object."""
        return cls(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
            use_starttls=settings.smtp_use_starttls,
            from_addr=settings.smtp_from_addr or settings.smtp_username,
            to_addrs=settings.digest_recipients,
        )


# ── Email builder ─────────────────────────────────────────────────────────────

class EmailDigest:
    """Builds and sends the weekly email digest."""

    def __init__(self, db: Database, smtp: SmtpConfig) -> None:
        self._db   = db
        self._smtp = smtp

    def send(self) -> None:
        """Build and send the digest email right now."""
        data = self._gather_data()
        subject = self._subject(data)
        html    = self._render_html(data)
        plain   = self._render_plain(data)
        self._send_email(subject, html, plain)
        logger.info("Digest email sent to %s", self._smtp.to_addrs)

    def test_connection(self) -> bool:
        """Verify SMTP credentials without sending email. Returns True on success."""
        try:
            conn = self._connect()
            conn.quit()
            logger.info("SMTP connection test OK.")
            return True
        except Exception as exc:
            logger.error("SMTP connection test FAILED: %s", exc)
            return False

    # ── Data gathering ────────────────────────────────────────────────────────

    def _gather_data(self) -> dict[str, Any]:
        summary   = self._db.get_full_stats_summary()
        per_day   = self._db.get_stars_per_day(days=7)
        by_lang   = self._db.get_language_breakdown()[:8]
        by_source = self._db.get_source_breakdown()
        top_repos = self._db.get_top_starred_repos(limit=10)
        batches   = self._db.get_batch_performance(limit=7)
        cumulative = self._db.get_cumulative_stars()
        return {
            "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "summary":      summary,
            "per_day":      per_day,
            "by_lang":      by_lang,
            "by_source":    by_source,
            "top_repos":    top_repos,
            "batches":      batches,
            "total_cumulative": cumulative[-1]["cumulative"] if cumulative else 0,
        }

    # ── Subject ───────────────────────────────────────────────────────────────

    def _subject(self, data: dict) -> str:
        week = data["summary"]["starred_this_week"]
        total = data["summary"]["total_starred"]
        return f"⭐ gh-autostar weekly digest — {week} new stars this week ({total:,} total)"

    # ── Plain text ────────────────────────────────────────────────────────────

    def _render_plain(self, data: dict) -> str:
        s = data["summary"]
        lines = [
            "gh-autostar Weekly Digest",
            "=" * 40,
            f"Generated: {data['generated_at']}",
            "",
            "SUMMARY",
            f"  Starred this week : {s['starred_this_week']}",
            f"  Starred today     : {s['starred_today']}",
            f"  Total starred     : {s['total_starred']:,}",
            f"  Total failed      : {s['total_failed']}",
            f"  Total batches     : {s['total_batches']}",
            "",
            "ACTIVITY THIS WEEK",
        ]
        for d in data["per_day"]:
            bar = "█" * min(d["count"], 30)
            lines.append(f"  {d['date']}  {bar} {d['count']}")

        lines += ["", "TOP LANGUAGES"]
        for l in data["by_lang"][:6]:
            lines.append(f"  {l['language']:<20} {l['count']:>4} repos")

        lines += ["", "TOP REPOS THIS WEEK"]
        for r in data["top_repos"][:8]:
            lines.append(f"  ⭐{int(r.get('stars', 0)):>7,}  {r['repo_full_name']}")
            lines.append(f"           https://github.com/{r['repo_full_name']}")

        lines += ["", "---", "gh-autostar | https://github.com/gh-autostar/gh-autostar"]
        return "\n".join(lines)

    # ── HTML ──────────────────────────────────────────────────────────────────

    def _render_html(self, data: dict) -> str:
        s = data["summary"]

        # Mini sparkline SVG for daily activity
        sparkline = self._sparkline_svg(data["per_day"])

        # Language bars
        lang_bars = self._lang_bars_html(data["by_lang"])

        # Source breakdown
        source_html = self._source_breakdown_html(data["by_source"])

        # Top repos rows
        repo_rows = "\n".join(
            f"""
            <tr>
              <td style="padding:8px 12px;">
                <a href="https://github.com/{r['repo_full_name']}"
                   style="color:#58a6ff;text-decoration:none;font-weight:500;">
                  {r['repo_full_name']}
                </a>
              </td>
              <td style="padding:8px 12px;color:#ffa657;text-align:right;">
                ⭐ {int(r.get('stars', 0)):,}
              </td>
              <td style="padding:8px 12px;color:#8b949e;">
                {r.get('language', '—')}
              </td>
              <td style="padding:8px 12px;color:#8b949e;font-size:12px;">
                {r.get('starred_at', '')[:10]}
              </td>
            </tr>
            """
            for r in data["top_repos"]
        )

        # Batch summary rows
        batch_rows = "\n".join(
            f"""
            <tr>
              <td style="padding:6px 10px;color:#8b949e;font-size:12px;">#{b['id']}</td>
              <td style="padding:6px 10px;font-size:12px;">{b['started_at'][:16]}</td>
              <td style="padding:6px 10px;color:#3fb950;text-align:right;">{b['total_starred']}</td>
              <td style="padding:6px 10px;color:#f78166;text-align:right;">{b['total_failed']}</td>
              <td style="padding:6px 10px;color:#8b949e;text-align:right;">{b['api_calls_used']}</td>
            </tr>
            """
            for b in data["batches"]
        )

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gh-autostar digest</title>
</head>
<body style="margin:0;padding:0;background:#0d1117;font-family:'Segoe UI',Arial,sans-serif;color:#e6edf3;">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:720px;margin:0 auto;">

    <!-- Header -->
    <tr><td style="background:#161b22;border-bottom:1px solid #21262d;padding:24px 32px;">
      <h1 style="margin:0;font-size:22px;color:#58a6ff;">⭐ gh-autostar</h1>
      <p style="margin:4px 0 0;color:#8b949e;font-size:13px;">Weekly Digest · {data["generated_at"]}</p>
    </td></tr>

    <!-- Summary cards -->
    <tr><td style="padding:24px 32px 0;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          {self._stat_card("This Week", s["starred_this_week"], "#3fb950")}
          {self._stat_card("Today", s["starred_today"], "#58a6ff")}
          {self._stat_card("Total Stars", f"{s['total_starred']:,}", "#ffa657")}
          {self._stat_card("Batches Run", s["total_batches"], "#d2a8ff")}
        </tr>
      </table>
    </td></tr>

    <!-- Sparkline -->
    <tr><td style="padding:24px 32px 0;">
      <h2 style="margin:0 0 12px;font-size:15px;color:#8b949e;font-weight:500;">
        DAILY ACTIVITY (LAST 7 DAYS)
      </h2>
      {sparkline}
    </td></tr>

    <!-- Language breakdown -->
    <tr><td style="padding:24px 32px 0;">
      <h2 style="margin:0 0 12px;font-size:15px;color:#8b949e;font-weight:500;">
        TOP LANGUAGES
      </h2>
      {lang_bars}
    </td></tr>

    <!-- Source breakdown -->
    <tr><td style="padding:24px 32px 0;">
      <h2 style="margin:0 0 12px;font-size:15px;color:#8b949e;font-weight:500;">
        DISCOVERY SOURCES
      </h2>
      {source_html}
    </td></tr>

    <!-- Top repos -->
    <tr><td style="padding:24px 32px 0;">
      <h2 style="margin:0 0 12px;font-size:15px;color:#8b949e;font-weight:500;">
        🏆 TOP STARRED REPOS
      </h2>
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;background:#161b22;border-radius:8px;overflow:hidden;">
        <thead>
          <tr style="border-bottom:1px solid #21262d;">
            <th style="padding:8px 12px;text-align:left;color:#8b949e;font-size:12px;">Repository</th>
            <th style="padding:8px 12px;text-align:right;color:#8b949e;font-size:12px;">Stars</th>
            <th style="padding:8px 12px;color:#8b949e;font-size:12px;">Language</th>
            <th style="padding:8px 12px;color:#8b949e;font-size:12px;">Date</th>
          </tr>
        </thead>
        <tbody>{repo_rows}</tbody>
      </table>
    </td></tr>

    <!-- Batch history -->
    <tr><td style="padding:24px 32px 0;">
      <h2 style="margin:0 0 12px;font-size:15px;color:#8b949e;font-weight:500;">
        ⚙️ RECENT BATCH RUNS
      </h2>
      <table width="100%" cellpadding="0" cellspacing="0"
             style="border-collapse:collapse;background:#161b22;border-radius:8px;overflow:hidden;">
        <thead>
          <tr style="border-bottom:1px solid #21262d;">
            <th style="padding:6px 10px;text-align:left;color:#8b949e;font-size:11px;">#</th>
            <th style="padding:6px 10px;text-align:left;color:#8b949e;font-size:11px;">Started</th>
            <th style="padding:6px 10px;text-align:right;color:#8b949e;font-size:11px;">Starred</th>
            <th style="padding:6px 10px;text-align:right;color:#8b949e;font-size:11px;">Failed</th>
            <th style="padding:6px 10px;text-align:right;color:#8b949e;font-size:11px;">API calls</th>
          </tr>
        </thead>
        <tbody>{batch_rows}</tbody>
      </table>
    </td></tr>

    <!-- Footer -->
    <tr><td style="padding:32px;border-top:1px solid #21262d;margin-top:32px;text-align:center;">
      <p style="margin:0;color:#8b949e;font-size:12px;">
        gh-autostar &nbsp;·&nbsp;
        <a href="https://github.com/gh-autostar/gh-autostar"
           style="color:#58a6ff;text-decoration:none;">GitHub</a>
        &nbsp;·&nbsp; Unsubscribe: set <code>digest_enabled = false</code>
      </p>
    </td></tr>

  </table>
</body>
</html>"""

    # ── HTML sub-components ───────────────────────────────────────────────────

    @staticmethod
    def _stat_card(label: str, value: Any, color: str) -> str:
        return f"""
        <td style="padding:0 8px 0 0;">
          <div style="background:#161b22;border:1px solid #21262d;border-radius:8px;
                      padding:14px 18px;text-align:center;">
            <div style="color:#8b949e;font-size:11px;margin-bottom:4px;">{label}</div>
            <div style="color:{color};font-size:26px;font-weight:700;">{value}</div>
          </div>
        </td>"""

    @staticmethod
    def _sparkline_svg(per_day: list[dict]) -> str:
        """Inline SVG bar chart for daily activity."""
        if not per_day:
            return "<p style='color:#8b949e;font-size:13px;'>No activity data.</p>"
        max_count = max(d["count"] for d in per_day) or 1
        w, h = 600, 80
        bar_w = w // len(per_day) - 4

        bars = []
        for i, d in enumerate(per_day):
            bh = int(d["count"] / max_count * (h - 20))
            x = i * (bar_w + 4) + 2
            y = h - bh - 16
            bars.append(
                f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bh}" '
                f'rx="3" fill="#58a6ff" opacity="0.8"/>'
                f'<text x="{x + bar_w//2}" y="{h - 2}" '
                f'fill="#8b949e" font-size="9" text-anchor="middle">'
                f'{d["date"][5:]}</text>'
            )
            if d["count"] > 0:
                bars.append(
                    f'<text x="{x + bar_w//2}" y="{y - 3}" '
                    f'fill="#e6edf3" font-size="9" text-anchor="middle">'
                    f'{d["count"]}</text>'
                )

        svg_content = "".join(bars)
        return (
            f'<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" '
            f'style="background:#161b22;border-radius:8px;display:block;">'
            f'{svg_content}</svg>'
        )

    @staticmethod
    def _lang_bars_html(by_lang: list[dict]) -> str:
        if not by_lang:
            return "<p style='color:#8b949e;'>No language data.</p>"
        max_count = max(l["count"] for l in by_lang) or 1
        colors = ["#58a6ff","#3fb950","#ffa657","#d2a8ff",
                  "#f78166","#79c0ff","#56d364","#e3b341"]
        rows = []
        for i, l in enumerate(by_lang):
            pct = int(l["count"] / max_count * 100)
            color = colors[i % len(colors)]
            rows.append(f"""
            <div style="margin-bottom:8px;">
              <div style="display:flex;justify-content:space-between;
                          margin-bottom:4px;font-size:13px;">
                <span>{l['language']}</span>
                <span style="color:#8b949e;">{l['count']} repos</span>
              </div>
              <div style="background:#21262d;border-radius:4px;height:8px;">
                <div style="background:{color};width:{pct}%;height:8px;
                            border-radius:4px;transition:width 0.3s;"></div>
              </div>
            </div>""")
        return "".join(rows)

    @staticmethod
    def _source_breakdown_html(by_source: list[dict]) -> str:
        if not by_source:
            return "<p style='color:#8b949e;'>No source data.</p>"
        total = sum(s["count"] for s in by_source) or 1
        colors = ["#58a6ff","#3fb950","#ffa657","#d2a8ff","#f78166"]
        items = []
        for i, s in enumerate(by_source):
            pct = s["count"] / total * 100
            color = colors[i % len(colors)]
            items.append(
                f'<div style="display:inline-block;margin-right:16px;font-size:13px;">'
                f'<span style="color:{color};font-weight:600;">{pct:.1f}%</span> '
                f'{s["source"]} ({s["count"]})</div>'
            )
        return "".join(items)

    # ── SMTP ──────────────────────────────────────────────────────────────────

    def _connect(self) -> smtplib.SMTP | smtplib.SMTP_SSL:
        ctx = ssl.create_default_context()
        if self._smtp.use_tls:
            conn: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(
                self._smtp.host, self._smtp.port, context=ctx
            )
        else:
            conn = smtplib.SMTP(self._smtp.host, self._smtp.port)
            if self._smtp.use_starttls:
                conn.starttls(context=ctx)

        conn.login(self._smtp.username, self._smtp.password)
        return conn

    def _send_email(self, subject: str, html: str, plain: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = self._smtp.from_addr
        msg["To"]      = ", ".join(self._smtp.to_addrs)
        msg["X-Mailer"] = "gh-autostar/1.0"

        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))

        conn = self._connect()
        try:
            conn.sendmail(self._smtp.from_addr, self._smtp.to_addrs, msg.as_string())
        finally:
            conn.quit()

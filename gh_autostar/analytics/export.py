"""
Export starred repos to JSON, CSV, or Markdown.

Sources:
  1. GitHub API  — live, always fresh
  2. Local DB    — offline, from repo_cache + star_records
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from gh_autostar.logging_setup import get_logger
from gh_autostar.models import Repository
from gh_autostar.storage.database import Database

logger = get_logger("export")

ExportFormat = Literal["json", "csv", "markdown"]


def export_stars(
    db: Database,
    output_path: Path,
    fmt: ExportFormat = "json",
    source: Literal["db", "api"] = "db",
    client: "object | None" = None,
    include_description: bool = True,
    include_topics: bool = True,
    group_by_language: bool = False,
) -> int:
    """
    Export all starred repos to a file.

    Args:
        db:                  Database instance (for 'db' source).
        output_path:         Destination file path.
        fmt:                 'json' | 'csv' | 'markdown'
        source:              'db' (local cache) or 'api' (live GitHub).
        client:              GitHubClient instance (required when source='api').
        include_description: Include repo descriptions.
        include_topics:      Include repo topics.
        group_by_language:   Group entries by language (markdown only).

    Returns:
        Number of repos exported.
    """
    if source == "api":
        if client is None:
            raise ValueError("client is required when source='api'")
        repos = _fetch_from_api(client)
    else:
        repos = _fetch_from_db(db)

    if not repos:
        logger.warning("No repos found to export.")
        output_path.write_text("[]" if fmt == "json" else "", encoding="utf-8")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)

    match fmt:
        case "json":
            _write_json(repos, output_path, include_description, include_topics)
        case "csv":
            _write_csv(repos, output_path, include_description, include_topics)
        case "markdown":
            _write_markdown(
                repos, output_path,
                include_description, include_topics, group_by_language,
            )

    logger.info("Exported %d repos to %s (%s)", len(repos), output_path, fmt)
    return len(repos)


# ── Data sources ──────────────────────────────────────────────────────────────

def _fetch_from_api(client: "object") -> list[dict]:
    """Fetch all starred repos live from GitHub API."""
    logger.info("Fetching starred repos from GitHub API…")
    repos = []
    for payload in client.get_starred_repos(per_page=100):  # type: ignore[attr-defined]
        repos.append({
            "full_name":         payload.get("full_name", ""),
            "name":              payload.get("name", ""),
            "owner":             (payload.get("owner") or {}).get("login", ""),
            "description":       payload.get("description") or "",
            "language":          payload.get("language") or "Unknown",
            "stargazers_count":  payload.get("stargazers_count", 0),
            "forks_count":       payload.get("forks_count", 0),
            "topics":            payload.get("topics", []),
            "html_url":          payload.get("html_url", ""),
            "is_fork":           payload.get("fork", False),
            "is_archived":       payload.get("archived", False),
            "pushed_at":         payload.get("pushed_at", ""),
            "created_at":        payload.get("created_at", ""),
            "license":           (payload.get("license") or {}).get("name", ""),
        })
    logger.info("Fetched %d starred repos from API.", len(repos))
    return repos


def _fetch_from_db(db: Database) -> list[dict]:
    """Build export data from star_records joined with repo_cache."""
    logger.info("Fetching starred repos from local DB…")
    top = db.get_top_starred_repos(limit=10_000)
    repos = []
    for r in top:
        # Try to get full cached data
        cached = db.get_cached_repo(r["repo_full_name"])
        if cached:
            repos.append({
                "full_name":        cached.full_name,
                "name":             cached.name,
                "owner":            cached.owner,
                "description":      cached.description or "",
                "language":         cached.language or "Unknown",
                "stargazers_count": cached.stargazers_count,
                "forks_count":      cached.forks_count,
                "topics":           cached.topics,
                "html_url":         cached.html_url,
                "is_fork":          cached.is_fork,
                "is_archived":      cached.is_archived,
                "pushed_at":        cached.pushed_at.isoformat() if cached.pushed_at else "",
                "created_at":       cached.created_at.isoformat() if cached.created_at else "",
                "license":          cached.license_name or "",
                "starred_at":       r.get("starred_at", ""),
                "source":           r.get("source", ""),
            })
        else:
            # Minimal record from star_records only
            repos.append({
                "full_name":        r["repo_full_name"],
                "name":             r["repo_full_name"].split("/")[-1],
                "owner":            r["repo_full_name"].split("/")[0],
                "description":      "",
                "language":         r.get("language", "Unknown"),
                "stargazers_count": r.get("stars", 0),
                "forks_count":      0,
                "topics":           [],
                "html_url":         f"https://github.com/{r['repo_full_name']}",
                "is_fork":          False,
                "is_archived":      False,
                "pushed_at":        "",
                "created_at":       "",
                "license":          "",
                "starred_at":       r.get("starred_at", ""),
                "source":           r.get("source", ""),
            })
    return repos


# ── Writers ───────────────────────────────────────────────────────────────────

def _write_json(
    repos: list[dict],
    path: Path,
    include_description: bool,
    include_topics: bool,
) -> None:
    export = []
    for r in repos:
        entry = {
            "full_name":        r["full_name"],
            "url":              r["html_url"],
            "language":         r["language"],
            "stars":            r["stargazers_count"],
            "forks":            r["forks_count"],
            "is_fork":          r["is_fork"],
            "is_archived":      r["is_archived"],
            "license":          r.get("license", ""),
            "starred_at":       r.get("starred_at", ""),
            "source":           r.get("source", ""),
        }
        if include_description:
            entry["description"] = r.get("description", "")
        if include_topics:
            entry["topics"] = r.get("topics", [])
        export.append(entry)

    meta = {
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
        "total":       len(export),
        "repos":       export,
    }
    path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_csv(
    repos: list[dict],
    path: Path,
    include_description: bool,
    include_topics: bool,
) -> None:
    fieldnames = [
        "full_name", "url", "language", "stars", "forks",
        "is_fork", "is_archived", "license", "starred_at", "source",
    ]
    if include_description:
        fieldnames.append("description")
    if include_topics:
        fieldnames.append("topics")

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in repos:
            row = {
                "full_name":   r["full_name"],
                "url":         r["html_url"],
                "language":    r["language"],
                "stars":       r["stargazers_count"],
                "forks":       r["forks_count"],
                "is_fork":     r["is_fork"],
                "is_archived": r["is_archived"],
                "license":     r.get("license", ""),
                "starred_at":  r.get("starred_at", ""),
                "source":      r.get("source", ""),
            }
            if include_description:
                row["description"] = r.get("description", "")
            if include_topics:
                row["topics"] = "|".join(r.get("topics", []))
            writer.writerow(row)


def _write_markdown(
    repos: list[dict],
    path: Path,
    include_description: bool,
    include_topics: bool,
    group_by_language: bool,
) -> None:
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = []

    lines.append(f"# ⭐ Starred Repositories")
    lines.append(f"")
    lines.append(f"> Exported {len(repos)} repos on {now}")
    lines.append(f"")

    if not group_by_language:
        lines.append("| Repository | Language | Stars | Forks |" +
                     (" Description |" if include_description else ""))
        lines.append("|-----------|----------|------:|------:|" +
                     ("-------------|" if include_description else ""))
        for r in repos:
            desc = r.get("description", "")[:80] if include_description else ""
            desc = desc.replace("|", "\\|")
            row = (
                f"| [{r['full_name']}]({r['html_url']}) "
                f"| {r['language']} "
                f"| ⭐ {r['stargazers_count']:,} "
                f"| 🍴 {r['forks_count']:,} "
            )
            if include_description:
                row += f"| {desc} "
            row += "|"
            lines.append(row)

            if include_topics and r.get("topics"):
                topics_str = " ".join(f"`{t}`" for t in r["topics"][:5])
                lines.append(f"| | *{topics_str}* | | |" +
                             (" |" if include_description else ""))
    else:
        # Group by language
        from collections import defaultdict
        by_lang: dict[str, list[dict]] = defaultdict(list)
        for r in repos:
            by_lang[r["language"] or "Unknown"].append(r)

        for lang in sorted(by_lang.keys()):
            lang_repos = by_lang[lang]
            lines.append(f"## {lang} ({len(lang_repos)})")
            lines.append("")
            for r in sorted(lang_repos, key=lambda x: x["stargazers_count"], reverse=True):
                desc = r.get("description", "") if include_description else ""
                desc_part = f" — {desc[:100]}" if desc else ""
                archived = " 🗄️" if r.get("is_archived") else ""
                topics = ""
                if include_topics and r.get("topics"):
                    topics = " " + " ".join(f"`{t}`" for t in r["topics"][:4])
                lines.append(
                    f"- [{r['full_name']}]({r['html_url']}) "
                    f"⭐{r['stargazers_count']:,}{archived}{desc_part}{topics}"
                )
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")

"""
Rich-based console output helpers for gh-autostar CLI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import humanize
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text
from rich import print as rprint

from gh_autostar.models import BatchResult, Repository, StarRecord, StarStatus

console = Console(stderr=False)
err_console = Console(stderr=True)


# ── Status symbols ────────────────────────────────────────────────────────────

_STATUS_STYLE: dict[StarStatus, tuple[str, str]] = {
    StarStatus.STARRED:         ("✓", "bold green"),
    StarStatus.ALREADY_STARRED: ("◈", "dim cyan"),
    StarStatus.SKIPPED:         ("○", "dim"),
    StarStatus.FAILED:          ("✗", "bold red"),
    StarStatus.FILTERED_OUT:    ("⊘", "dim yellow"),
    StarStatus.PENDING:         ("…", "dim white"),
}


def status_text(status: StarStatus) -> Text:
    sym, style = _STATUS_STYLE.get(status, ("?", "white"))
    return Text(f"{sym} {status.value}", style=style)


# ── Batch result summary ──────────────────────────────────────────────────────

def print_batch_summary(result: BatchResult) -> None:
    duration = result.duration_seconds
    dur_str = humanize.naturaldelta(duration) if duration else "—"

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column(style="bold")

    grid.add_row("Discovered",     str(result.total_discovered))
    grid.add_row("Starred",        f"[green]{result.total_starred}[/green]")
    grid.add_row("Already starred", f"[cyan]{result.total_already_starred}[/cyan]")
    grid.add_row("Filtered out",   f"[yellow]{result.total_filtered_out}[/yellow]")
    grid.add_row("Failed",         f"[red]{result.total_failed}[/red]" if result.total_failed else "0")
    grid.add_row("Duration",       dur_str)
    if result.api_calls_remaining is not None:
        grid.add_row("API remaining", str(result.api_calls_remaining))

    console.print(
        Panel(grid, title="[bold]Batch Result[/bold]", border_style="blue", expand=False)
    )


# ── Star records table ────────────────────────────────────────────────────────

def print_star_records(records: list[StarRecord], title: str = "Star Records") -> None:
    table = Table(
        title=title,
        box=box.ROUNDED,
        show_header=True,
        header_style="bold magenta",
        expand=False,
    )
    table.add_column("Repository", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Source", style="dim")
    table.add_column("When", style="dim", no_wrap=True)

    for r in records:
        sym, style = _STATUS_STYLE.get(r.status, ("?", "white"))
        when = humanize.naturaltime(r.starred_at)
        table.add_row(
            r.repo_full_name,
            Text(f"{sym} {r.status.value}", style=style),
            r.source,
            when,
        )

    console.print(table)


# ── Repo table ────────────────────────────────────────────────────────────────

def print_repo_table(repos: list[Repository], title: str = "Repositories") -> None:
    table = Table(
        title=title,
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold blue",
        expand=True,
    )
    table.add_column("Repository", style="cyan", no_wrap=True)
    table.add_column("⭐", justify="right")
    table.add_column("🍴", justify="right")
    table.add_column("Language", style="yellow")
    table.add_column("Topics", style="dim", max_width=40)

    for r in repos:
        table.add_row(
            r.full_name,
            humanize.intcomma(r.stargazers_count),
            humanize.intcomma(r.forks_count),
            r.language or "—",
            ", ".join(r.topics[:5]) or "—",
        )

    console.print(table)


# ── Batch history table ───────────────────────────────────────────────────────

def print_batch_history(runs: list[dict[str, Any]]) -> None:
    table = Table(
        title="Batch History",
        box=box.ROUNDED,
        header_style="bold magenta",
    )
    table.add_column("#", justify="right", style="dim")
    table.add_column("Started", style="cyan")
    table.add_column("Starred", justify="right", style="green")
    table.add_column("Already ★", justify="right", style="dim cyan")
    table.add_column("Filtered", justify="right", style="yellow")
    table.add_column("Failed", justify="right", style="red")
    table.add_column("API calls", justify="right", style="dim")

    for run in runs:
        table.add_row(
            str(run["id"]),
            humanize.naturaltime(datetime.fromisoformat(run["started_at"])),
            str(run["total_starred"]),
            str(run["total_already_starred"]),
            str(run["total_filtered_out"]),
            str(run["total_failed"]) if run["total_failed"] else "—",
            str(run["api_calls_used"]),
        )

    console.print(table)


# ── DB stats ──────────────────────────────────────────────────────────────────

def print_db_stats(stats: dict[str, int]) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim")
    grid.add_column(style="bold")
    for key, val in stats.items():
        grid.add_row(key.replace("_", " ").title(), humanize.intcomma(val))
    console.print(Panel(grid, title="[bold]Database Stats[/bold]", border_style="dim", expand=False))


# ── Progress context ──────────────────────────────────────────────────────────

def make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


def print_error(msg: str) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {msg}")


def print_success(msg: str) -> None:
    console.print(f"[bold green]✓[/bold green] {msg}")


def print_warning(msg: str) -> None:
    console.print(f"[bold yellow]⚠[/bold yellow] {msg}")


def print_info(msg: str) -> None:
    console.print(f"[dim]ℹ[/dim] {msg}")

"""
gh-autostar CLI — entry point.

Commands:
  auth        Manage GitHub authentication (keychain-backed)
  run         Execute one batch run immediately
  daemon      Manage the background daemon
  star        Star / unstar individual repos
  history     Browse batch history and star records
  config      Show / edit configuration
  cache       Manage the local SQLite cache
  status      Show current status, rate limits, security warnings
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.prompt import Confirm, Prompt

from gh_autostar._version import __version__
from gh_autostar.cli.context import AppContext
from gh_autostar.cli.output import (
    console,
    err_console,
    print_batch_history,
    print_batch_summary,
    print_db_stats,
    print_error,
    print_info,
    print_repo_table,
    print_star_records,
    print_success,
    print_warning,
)
from gh_autostar.config import get_settings
from gh_autostar.core.engine import AutoStarEngine
from gh_autostar.core.client import GitHubAPIError
from gh_autostar.logging_setup import setup_logging
from gh_autostar.models import StarStatus, parse_repo_slug
from gh_autostar.scheduler.daemon import AutoStarDaemon
from gh_autostar.scheduler.startup import StartupRegistrar
from gh_autostar.security import (
    AuditLogger,
    mask_token,
    token_fingerprint,
    validate_token_format,
    install_token_masking_filter,
)

app = typer.Typer(
    name="gh-autostar",
    help="Automated GitHub repo starring — batch-star, schedule, cache.",
    add_completion=True,
    rich_markup_mode="rich",
    no_args_is_help=True,
    invoke_without_command=True,
)

daemon_app  = typer.Typer(help="Manage the background scheduling daemon.", no_args_is_help=True)
auth_app    = typer.Typer(help="Manage GitHub authentication.", no_args_is_help=True)
star_app    = typer.Typer(help="Star or unstar repositories.", no_args_is_help=True)
history_app = typer.Typer(help="Browse batch history and star logs.", no_args_is_help=True)
cache_app   = typer.Typer(help="Manage the local discovery cache.", no_args_is_help=True)
config_app  = typer.Typer(help="Show and edit configuration.", no_args_is_help=True)

app.add_typer(daemon_app,  name="daemon")
app.add_typer(auth_app,    name="auth")
app.add_typer(star_app,    name="star")
app.add_typer(history_app, name="history")
app.add_typer(cache_app,   name="cache")
app.add_typer(config_app,  name="config")

_ctx = AppContext()


def _ensure_token() -> None:
    cfg = get_settings()
    if not cfg.token:
        print_error(
            "No GitHub token configured.\n"
            "Run [bold]gh-autostar auth login[/bold] or set "
            "[bold]GH_AUTOSTAR_GITHUB_TOKEN[/bold]."
        )
        raise typer.Exit(1)


def _audit() -> AuditLogger:
    cfg = get_settings()
    return AuditLogger(cfg.log_dir)


def _show_security_warnings() -> None:
    cfg = get_settings()
    for w in cfg.security_warnings():
        print_warning(w)


# ── Top-level callback ────────────────────────────────────────────────────────

@app.callback()
def _main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", "-V", is_eager=True)] = False,
) -> None:
    # Install token masking on every invocation
    install_token_masking_filter()

    if version:
        console.print(f"gh-autostar [cyan]{__version__}[/cyan]")
        raise typer.Exit()

    if ctx.invoked_subcommand is not None:
        _show_security_warnings()


# ── run ───────────────────────────────────────────────────────────────────────

@app.command()
def run(
    dry_run:    Annotated[bool,         typer.Option("--dry-run", "-n")] = False,
    batch_size: Annotated[Optional[int], typer.Option("--batch-size", "-b")] = None,
    verbose:    Annotated[bool,         typer.Option("--verbose", "-v")] = False,
) -> None:
    """Execute one auto-star batch run immediately."""
    _ensure_token()
    cfg = get_settings()
    setup_logging(level="DEBUG" if verbose else cfg.log_level, log_file=cfg.log_file)

    if batch_size:
        cfg.batch_size = batch_size  # type: ignore[assignment]

    audit = _audit()
    audit.log_startup(__version__, cfg.sources)

    with _ctx.client as client:
        engine = AutoStarEngine(settings=cfg, client=client, db=_ctx.db)
        try:
            result = engine.run_batch(dry_run=dry_run)
        except GitHubAPIError as exc:
            if exc.status == 401:
                console.print(
                    "\n[bold red]Authentication failed (401 Bad credentials)[/bold red]\n"
                    "Your GitHub token is invalid or has expired.\n\n"
                    "To fix:\n"
                    "  1. Go to https://github.com/settings/tokens and generate a new token\n"
                    "     with [bold]repo[/bold] and [bold]user[/bold] scopes.\n"
                    "  2. Run [bold]gh-autostar auth login[/bold] to store the new token.\n"
                )
                raise typer.Exit(1)
            raise

    # Audit each starred repo
    for record in result.records:
        audit.log_star(record.repo_full_name, record.status.value, record.source)

    print_batch_summary(result)
    if dry_run:
        print_warning("Dry-run mode — no repos were actually starred.")


# ── status ────────────────────────────────────────────────────────────────────

@app.command()
def status() -> None:
    """Show rate limits, DB stats, startup registration, and security posture."""
    _ensure_token()
    cfg = get_settings()

    # Rate limit
    with _ctx.client as client:
        rl_data = client.get_rate_limit()
    resources = rl_data.get("resources", {})
    core   = resources.get("core", {})
    search = resources.get("search", {})
    console.print(
        f"\n[bold]Rate Limits[/bold]\n"
        f"  core    {core.get('remaining', '?')}/{core.get('limit', '?')} remaining "
        f"(resets in {max(0, core.get('reset', 0) - __import__('time').time()):.0f}s)\n"
        f"  search  {search.get('remaining', '?')}/{search.get('limit', '?')} remaining"
    )

    # DB stats
    print_db_stats(_ctx.db.get_db_stats())

    # Startup
    reg = StartupRegistrar()
    registered = reg.is_registered()
    console.print(
        f"\n[bold]Startup[/bold]  "
        f"{'[green]registered[/green]' if registered else '[dim]not registered[/dim]'}"
    )

    # Token info
    token = cfg.token
    from gh_autostar.security import load_token_keychain
    in_keychain = bool(load_token_keychain())
    storage = "[green]OS keychain[/green]" if in_keychain else "[yellow].env file[/yellow]"
    console.print(
        f"\n[bold]Token[/bold]  {mask_token(token)}  "
        f"storage={storage}  "
        f"fingerprint={token_fingerprint(token)}"
    )

    # Security warnings
    warnings = cfg.security_warnings()
    if warnings:
        console.print("\n[bold yellow]Security Warnings[/bold yellow]")
        for w in warnings:
            console.print(f"  [yellow]⚠[/yellow] {w}")
    else:
        console.print("\n[bold green]✓ No security warnings[/bold green]")

    # Paths
    console.print(f"\n[bold]Paths[/bold]")
    console.print(f"  config  {cfg.config_dir}")
    console.print(f"  data    {cfg.data_dir}")
    console.print(f"  db      {cfg.database_path}")
    console.print(f"  log     {cfg.log_file}")
    console.print(f"  audit   {cfg.log_dir / 'audit.log'}\n")


# ── auth commands ─────────────────────────────────────────────────────────────

@auth_app.command("login")
def auth_login(
    token: Annotated[Optional[str], typer.Option("--token", "-t", help="GitHub PAT (avoid — prefer interactive prompt).")] = None,
) -> None:
    """
    Save a GitHub Personal Access Token securely.

    Token is stored in the OS keychain (Windows Credential Manager /
    macOS Keychain / Linux Secret Service) when available.
    Falls back to a chmod-600 .env file.
    """
    if token:
        print_warning(
            "Passing token via --token flag is less secure (visible in shell history). "
            "Consider using the interactive prompt instead."
        )
    else:
        token = Prompt.ask(
            "GitHub Personal Access Token",
            password=True,  # hides input
        )
    if not token:
        print_error("Token cannot be empty.")
        raise typer.Exit(1)

    token = token.strip()

    # Validate format
    if not validate_token_format(token):
        print_warning(
            "Token format not recognised (expected ghp_*, github_pat_*, etc.). "
            "Proceeding anyway — verify it works."
        )

    cfg = get_settings()

    # Verify token against GitHub API before storing
    try:
        from gh_autostar.core.client import GitHubClient
        with GitHubClient(token=token) as c:
            user = c.get_authenticated_user()
    except Exception as exc:
        print_error(f"Token verification failed: {exc}")
        raise typer.Exit(1)

    username = user["login"]

    # Store securely
    used_keychain = cfg.save_token(token)

    storage_msg = (
        "[green]OS keychain[/green]" if used_keychain
        else "[yellow].env file (keychain unavailable)[/yellow]"
    )
    print_success(f"Authenticated as [bold]{username}[/bold]")
    console.print(f"  Token stored in: {storage_msg}")
    console.print(f"  Fingerprint:     {token_fingerprint(token)}")

    cfg.save_env(github_username=username)

    # Audit
    _audit().log_auth(username, token_fingerprint(token), success=True)


@auth_app.command("logout")
def auth_logout() -> None:
    """Remove the saved GitHub token from keychain and .env."""
    cfg = get_settings()
    from gh_autostar.security import delete_token_keychain
    keychain_removed = delete_token_keychain()
    cfg._remove_from_env("github_token")
    cfg.save_env(github_username="")

    if keychain_removed:
        print_success("Token removed from OS keychain.")
    else:
        print_success("Token removed from .env file.")


@auth_app.command("whoami")
def auth_whoami() -> None:
    """Show the currently authenticated GitHub user."""
    _ensure_token()
    cfg = get_settings()
    with _ctx.client as client:
        user = client.get_authenticated_user()

    console.print(f"\n[bold]{user['login']}[/bold]  {user.get('name', '')}  <{user.get('email') or '—'}>")
    console.print(f"Public repos: {user.get('public_repos', 0)}  Followers: {user.get('followers', 0)}")
    console.print(f"Token: {mask_token(cfg.token)}  fingerprint={token_fingerprint(cfg.token)}\n")


@auth_app.command("migrate-keychain")
def auth_migrate_keychain() -> None:
    """
    Move token from .env plaintext file into the OS keychain.
    Removes it from .env after successful migration.
    """
    cfg = get_settings()
    env_path = cfg.config_dir / ".env"

    # Find token in .env
    token_in_env = ""
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("GH_AUTOSTAR_GITHUB_TOKEN="):
                token_in_env = line.split("=", 1)[1].strip()
                break

    if not token_in_env:
        print_info("No token found in .env file.")
        return

    from gh_autostar.security import store_token_keychain
    if store_token_keychain(token_in_env):
        cfg._remove_from_env("github_token")
        print_success(
            f"Token migrated to OS keychain. "
            f"Fingerprint: {token_fingerprint(token_in_env)}"
        )
    else:
        print_error("OS keychain unavailable. Token remains in .env.")
        raise typer.Exit(1)


@auth_app.command("security-check")
def auth_security_check() -> None:
    """Run a full security audit of the current token setup."""
    cfg = get_settings()
    from gh_autostar.security import (
        check_env_file_permissions, load_token_keychain, validate_token_format
    )

    console.print("\n[bold]gh-autostar Security Check[/bold]\n")

    token = cfg.token
    in_keychain = bool(load_token_keychain())

    checks = [
        ("Token present",         bool(token),                        "No token configured"),
        ("Token format valid",    validate_token_format(token) if token else False, "Unrecognised token format"),
        ("Stored in keychain",    in_keychain,                        "Token in plaintext .env (run auth migrate-keychain)"),
        ("Audit log enabled",     cfg.audit_log_enabled,              "Audit log is disabled"),
    ]

    env_path = cfg.config_dir / ".env"
    perm_warns = check_env_file_permissions(env_path)
    checks.append((".env permissions OK", len(perm_warns) == 0, perm_warns[0] if perm_warns else ""))

    all_ok = True
    for label, ok, warn in checks:
        if ok:
            console.print(f"  [green]✓[/green] {label}")
        else:
            console.print(f"  [red]✗[/red] {label}  → {warn}")
            all_ok = False

    console.print()
    if all_ok:
        console.print("[bold green]All checks passed.[/bold green]\n")
    else:
        console.print("[bold yellow]Some checks failed — see above.[/bold yellow]\n")
        raise typer.Exit(1)


# ── star commands ─────────────────────────────────────────────────────────────

@star_app.command("add")
def star_add(
    repos:   Annotated[list[str], typer.Argument(help="'owner/repo' slugs to star.")],
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n")] = False,
) -> None:
    """Star one or more repositories immediately."""
    _ensure_token()
    from gh_autostar.security import sanitise_repo_slug
    cfg = get_settings()
    audit = _audit()
    with _ctx.client as client:
        engine = AutoStarEngine(settings=cfg, client=client, db=_ctx.db)
        for slug in repos:
            try:
                slug = sanitise_repo_slug(slug)
                owner, repo = parse_repo_slug(slug)
            except ValueError as exc:
                print_error(str(exc))
                continue
            record = engine.star_single(owner, repo, dry_run=dry_run)
            console.print(f"  {slug}  →  {record.status.value}")
            if not dry_run:
                audit.log_star(slug, record.status.value, "manual")


@star_app.command("remove")
def star_remove(
    repos: Annotated[list[str], typer.Argument(help="'owner/repo' slugs to unstar.")],
) -> None:
    """Unstar one or more repositories."""
    _ensure_token()
    from gh_autostar.security import sanitise_repo_slug
    audit = _audit()
    with _ctx.client as client:
        for slug in repos:
            try:
                slug = sanitise_repo_slug(slug)
                owner, repo = parse_repo_slug(slug)
            except ValueError as exc:
                print_error(str(exc))
                continue
            client.unstar_repo(owner, repo)
            _ctx.db.delete_star_record(slug)
            audit.log_unstar(slug)
            print_success(f"Unstarred {slug}")


@star_app.command("check")
def star_check(
    repos: Annotated[list[str], typer.Argument(help="'owner/repo' slugs to check.")],
) -> None:
    """Check whether repos are already starred."""
    _ensure_token()
    from gh_autostar.security import sanitise_repo_slug
    with _ctx.client as client:
        for slug in repos:
            try:
                slug = sanitise_repo_slug(slug)
                owner, repo = parse_repo_slug(slug)
            except ValueError as exc:
                print_error(str(exc))
                continue
            starred = client.is_starred(owner, repo)
            mark = "[green]★[/green]" if starred else "[dim]☆[/dim]"
            console.print(f"  {mark}  {slug}")


# ── history commands ──────────────────────────────────────────────────────────

@history_app.command("runs")
def history_runs(limit: Annotated[int, typer.Option("--limit", "-n")] = 20) -> None:
    """Show recent batch run history."""
    runs = _ctx.db.get_batch_runs(limit=limit)
    if not runs:
        print_info("No batch runs recorded yet.")
        return
    print_batch_history(runs)


@history_app.command("starred")
def history_starred(
    limit:  Annotated[int, typer.Option("--limit", "-n")] = 50,
    offset: Annotated[int, typer.Option("--offset")] = 0,
) -> None:
    """Show repos that were successfully starred."""
    records = _ctx.db.get_star_records(status=StarStatus.STARRED, limit=limit, offset=offset)
    if not records:
        print_info("No starred repos recorded yet.")
        return
    print_star_records(records, title=f"Starred Repos ({len(records)})")


@history_app.command("failed")
def history_failed(limit: Annotated[int, typer.Option("--limit", "-n")] = 50) -> None:
    """Show repos that failed to star."""
    records = _ctx.db.get_star_records(status=StarStatus.FAILED, limit=limit)
    if not records:
        print_info("No failures recorded.")
        return
    print_star_records(records, title="Failed Star Attempts")


@history_app.command("all")
def history_all(
    limit:  Annotated[int, typer.Option("--limit", "-n")] = 100,
    status: Annotated[Optional[str], typer.Option("--status")] = None,
) -> None:
    """Show all star records."""
    st = StarStatus(status) if status else None
    records = _ctx.db.get_star_records(status=st, limit=limit)
    if not records:
        print_info("No records found.")
        return
    print_star_records(records, title=f"All Records (limit {limit})")


@history_app.command("audit")
def history_audit(
    lines: Annotated[int, typer.Option("--lines", "-n")] = 50,
) -> None:
    """Show the security audit log."""
    cfg = get_settings()
    audit_path = cfg.log_dir / "audit.log"
    if not audit_path.exists():
        print_info("Audit log is empty.")
        return
    all_lines = audit_path.read_text(encoding="utf-8").splitlines()
    for line in all_lines[-lines:]:
        console.print(line)


# ── cache commands ────────────────────────────────────────────────────────────

@cache_app.command("show")
def cache_show() -> None:
    """Show repos currently in the discovery cache."""
    repos = _ctx.db.get_cached_repos()
    if not repos:
        print_info("Cache is empty.")
        return
    print_repo_table(repos, title=f"Cached Repos ({len(repos)})")


@cache_app.command("prune")
def cache_prune() -> None:
    """Remove expired entries from the repo cache."""
    n = _ctx.db.prune_expired_cache()
    print_success(f"Pruned {n} expired cache entries.")


@cache_app.command("clear")
def cache_clear(yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Clear ALL cache entries."""
    if not yes and not Confirm.ask("[yellow]Clear all cache?[/yellow]"):
        raise typer.Exit()
    _ctx.db.invalidate_starred_cache()
    _ctx.db.prune_expired_cache()
    print_success("Cache cleared.")


@cache_app.command("vacuum")
def cache_vacuum() -> None:
    """Run VACUUM on the SQLite database."""
    _ctx.db.vacuum()
    print_success("VACUUM complete.")


# ── config commands ───────────────────────────────────────────────────────────

@config_app.command("show")
def config_show() -> None:
    """Print current effective configuration (token is masked)."""
    cfg = get_settings()
    from rich.table import Table
    from rich import box as rbox
    table = Table(box=rbox.SIMPLE, show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(style="bold")

    data = cfg.model_dump()
    for key in sorted(cfg.model_fields):
        val = data[key]
        # Always mask token
        if key == "github_token":
            val = mask_token(cfg.token)
        table.add_row(key, str(val))

    console.print(table)


@config_app.command("set")
def config_set(
    key:   Annotated[str, typer.Argument()],
    value: Annotated[str, typer.Argument()],
) -> None:
    """Persist a configuration value."""
    cfg = get_settings()
    if key in ("github_token", "token"):
        print_error("Use 'gh-autostar auth login' to set the token securely.")
        raise typer.Exit(1)
    try:
        cfg.save_env(**{key: value})
        _audit().log_config_change(key, value[:40])
        print_success(f"Set {key} = {value}")
    except Exception as exc:
        print_error(str(exc))
        raise typer.Exit(1)


@config_app.command("path")
def config_path() -> None:
    """Print the path to the config directory."""
    cfg = get_settings()
    console.print(str(cfg.config_dir))


# ── daemon commands ───────────────────────────────────────────────────────────

@daemon_app.command("start")
def daemon_start(
    foreground: Annotated[bool, typer.Option("--foreground", "-f")] = True,
) -> None:
    """Start the background scheduling daemon."""
    _ensure_token()
    cfg = get_settings()
    setup_logging(level=cfg.log_level, log_file=cfg.log_file)
    install_token_masking_filter()

    daemon = AutoStarDaemon(settings=cfg)

    if cfg.run_on_startup:
        reg = StartupRegistrar()
        if not reg.is_registered():
            try:
                reg.register()
                print_success("Startup entry registered.")
            except Exception as exc:
                print_warning(f"Could not register startup entry: {exc}")

    console.print(
        f"[bold green]Daemon starting[/bold green]  "
        f"interval=[cyan]{cfg.scheduler_interval_minutes}m[/cyan]  "
        f"sources={cfg.sources}"
    )
    daemon.start(foreground=foreground)


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the daemon."""
    import os, signal
    pid_file = get_settings().data_dir / "daemon.pid"
    if not pid_file.exists():
        print_warning("No PID file found.")
        return
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print_success(f"Sent SIGTERM to daemon (PID {pid}).")
    except ProcessLookupError:
        print_warning(f"No process found with PID {pid}.")
        pid_file.unlink(missing_ok=True)


@daemon_app.command("enable-startup")
def daemon_enable_startup() -> None:
    """Register gh-autostar to run on login."""
    reg = StartupRegistrar()
    if reg.is_registered():
        print_info("Already registered.")
        return
    reg.register()
    print_success("Startup entry registered.")


@daemon_app.command("disable-startup")
def daemon_disable_startup() -> None:
    """Remove OS startup entry."""
    reg = StartupRegistrar()
    if not reg.is_registered():
        print_info("Not registered.")
        return
    reg.unregister()
    print_success("Startup entry removed.")


@daemon_app.command("status")
def daemon_status() -> None:
    """Check startup registration."""
    reg = StartupRegistrar()
    if reg.is_registered():
        print_success("Startup entry [bold]registered[/bold].")
    else:
        print_warning("Startup entry [bold]not registered[/bold].")


# ── stats command ─────────────────────────────────────────────────────────────

@app.command()
def stats(
    port:    Annotated[int,  typer.Option("--port", "-p")] = 8751,
    no_browser: Annotated[bool, typer.Option("--no-browser")] = False,
) -> None:
    """Launch the interactive analytics dashboard (Dash + Plotly)."""
    try:
        from gh_autostar.analytics.dashboard import launch_dashboard
    except ImportError:
        print_error("Dashboard requires: pip install dash plotly pandas")
        raise typer.Exit(1)

    cfg = get_settings()
    db = _ctx.db
    console.print(f"[bold green]Dashboard starting[/bold green] → http://127.0.0.1:{port}")
    launch_dashboard(db=db, port=port, open_browser=not no_browser)


# ── export command ────────────────────────────────────────────────────────────

@app.command()
def export(
    output:  Annotated[str, typer.Argument(help="Output file path.")] = "stars.json",
    fmt:     Annotated[str, typer.Option("--format", "-f",
             help="json | csv | markdown")] = "json",
    source:  Annotated[str, typer.Option("--source", "-s",
             help="db (local cache) | api (live GitHub)")] = "db",
    group:   Annotated[bool, typer.Option("--group-by-language", "-g")] = False,
    no_desc: Annotated[bool, typer.Option("--no-description")] = False,
    no_topics: Annotated[bool, typer.Option("--no-topics")] = False,
) -> None:
    """Export starred repos to JSON, CSV, or Markdown."""
    from gh_autostar.analytics.export import export_stars, ExportFormat

    if fmt not in ("json", "csv", "markdown"):
        print_error("--format must be one of: json, csv, markdown")
        raise typer.Exit(1)
    if source not in ("db", "api"):
        print_error("--source must be one of: db, api")
        raise typer.Exit(1)
    if source == "api":
        _ensure_token()

    out_path = Path(output)

    client = _ctx.client if source == "api" else None
    n = export_stars(
        db=_ctx.db,
        output_path=out_path,
        fmt=fmt,  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        client=client,
        include_description=not no_desc,
        include_topics=not no_topics,
        group_by_language=group,
    )
    print_success(f"Exported {n} repos → {out_path}")


# ── digest commands ───────────────────────────────────────────────────────────

digest_app = typer.Typer(help="Manage weekly email digest.", no_args_is_help=True)
app.add_typer(digest_app, name="digest")


@digest_app.command("send")
def digest_send() -> None:
    """Send the weekly email digest right now."""
    from gh_autostar.analytics.digest import EmailDigest, SmtpConfig
    cfg = get_settings()
    if not cfg.smtp_username:
        print_error("SMTP not configured. Run: gh-autostar digest setup")
        raise typer.Exit(1)
    smtp = SmtpConfig.from_settings(cfg)
    digest = EmailDigest(db=_ctx.db, smtp=smtp)
    with console.status("Sending digest…"):
        digest.send()
    print_success(f"Digest sent to {cfg.digest_recipients}")


@digest_app.command("test")
def digest_test() -> None:
    """Test SMTP connection without sending."""
    from gh_autostar.analytics.digest import EmailDigest, SmtpConfig
    cfg = get_settings()
    if not cfg.smtp_username:
        print_error("SMTP not configured. Run: gh-autostar digest setup")
        raise typer.Exit(1)
    smtp = SmtpConfig.from_settings(cfg)
    digest = EmailDigest(db=_ctx.db, smtp=smtp)
    ok = digest.test_connection()
    if ok:
        print_success("SMTP connection OK.")
    else:
        print_error("SMTP connection failed. Check credentials.")
        raise typer.Exit(1)


@digest_app.command("setup")
def digest_setup() -> None:
    """Interactive SMTP setup wizard."""
    from rich.prompt import Prompt

    cfg = get_settings()
    console.print("\n[bold]Email Digest Setup[/bold]\n")

    provider = Prompt.ask(
        "Provider",
        choices=["gmail", "outlook", "custom"],
        default="gmail",
    )

    if provider == "gmail":
        console.print("[dim]Gmail requires an App Password (not your regular password).[/dim]")
        console.print("[dim]Enable at: myaccount.google.com → Security → App passwords[/dim]\n")
        username = Prompt.ask("Gmail address")
        password = Prompt.ask("App password", password=True)
        cfg.save_env(
            smtp_host="smtp.gmail.com",
            smtp_port=465,
            smtp_username=username,
            smtp_password=password,
            smtp_use_tls=True,
            smtp_use_starttls=False,
        )
    elif provider == "outlook":
        username = Prompt.ask("Outlook email")
        password = Prompt.ask("Password", password=True)
        cfg.save_env(
            smtp_host="smtp.office365.com",
            smtp_port=587,
            smtp_username=username,
            smtp_password=password,
            smtp_use_tls=False,
            smtp_use_starttls=True,
        )
    else:
        host = Prompt.ask("SMTP host")
        port = int(Prompt.ask("SMTP port", default="587"))
        username = Prompt.ask("Username")
        password = Prompt.ask("Password", password=True)
        use_tls = Prompt.ask("Use SSL/TLS?", choices=["y","n"], default="n") == "y"
        cfg.save_env(
            smtp_host=host,
            smtp_port=port,
            smtp_username=username,
            smtp_password=password,
            smtp_use_tls=use_tls,
            smtp_use_starttls=not use_tls,
        )

    from_addr = Prompt.ask("From address (leave blank = same as username)", default="")
    recipients_raw = Prompt.ask("Recipients (comma-separated)", default=username)
    recipients = [r.strip() for r in recipients_raw.split(",")]

    cfg.save_env(
        smtp_from_addr=from_addr,
        digest_recipients=recipients,
        digest_enabled=True,
    )
    print_success("SMTP configured.")

    # Test immediately
    if Prompt.ask("Test connection now?", choices=["y","n"], default="y") == "y":
        from gh_autostar.analytics.digest import EmailDigest, SmtpConfig
        cfg2 = get_settings(reload=True)
        smtp = SmtpConfig.from_settings(cfg2)
        ok = EmailDigest(db=_ctx.db, smtp=smtp).test_connection()
        if ok:
            print_success("Connection verified.")
        else:
            print_error("Connection failed — check credentials.")


@digest_app.command("status")
def digest_status() -> None:
    """Show digest configuration."""
    cfg = get_settings()
    from gh_autostar.security import mask_token
    console.print(f"\n[bold]Email Digest[/bold]")
    console.print(f"  Enabled:     {cfg.digest_enabled}")
    console.print(f"  Recipients:  {cfg.digest_recipients}")
    console.print(f"  Schedule:    {cfg.digest_day_of_week} at {cfg.digest_hour_utc:02d}:00 UTC")
    console.print(f"  SMTP host:   {cfg.smtp_host}:{cfg.smtp_port}")
    console.print(f"  Username:    {cfg.smtp_username}")
    password = cfg.smtp_password
    console.print(f"  Password:    {mask_token(password) if password else '(not set)'}\n")

# gh-autostar

**Automated GitHub repository starring** — batch-star repos on a schedule,
run on OS startup, cache everything in SQLite, and filter with precision.

---

## Features

| Feature | Details |
|---------|---------|
| **Batch starring** | Star up to N repos per run with configurable per-request delay |
| **Multiple discovery sources** | Trending, Explore, Following-starred, Topic search, Manual list |
| **Advanced filtering** | Stars, forks, language, topics (require-all / any-of), owner exclusions |
| **SQLite cache** | Discovered repos + current starred-list cached with TTL |
| **Rate-limit aware** | Tracks remaining API quota; backs off automatically |
| **Background daemon** | APScheduler-powered, runs every N minutes |
| **OS startup** | Registers itself via systemd (Linux), launchd (macOS), Task Scheduler (Windows) |
| **Retry logic** | Tenacity exponential back-off on transient failures |
| **Rich CLI** | Full-featured Typer CLI with coloured tables and progress |

---

## Installation

```bash
pip install gh-autostar
# or, for development:
git clone https://github.com/gh-autostar/gh-autostar
cd gh-autostar
pip install -e ".[dev]"
```

---

## Quick start

```bash
# 1. Save your GitHub PAT (needs repo + read:user scopes)
gh-autostar auth login

# 2. Run one batch immediately
gh-autostar run

# 3. Start the daemon (runs every 60 min by default)
gh-autostar daemon start
```

---

## Configuration

All settings can be set via environment variables (`GH_AUTOSTAR_*`),
a `.env` file (in the platform config dir), or `gh-autostar config set`.

| Variable | Default | Description |
|----------|---------|-------------|
| `GH_AUTOSTAR_GITHUB_TOKEN` | *(required)* | GitHub Personal Access Token |
| `GH_AUTOSTAR_BATCH_SIZE` | `30` | Repos to star per batch |
| `GH_AUTOSTAR_BATCH_DELAY_SECONDS` | `1.5` | Delay between star requests |
| `GH_AUTOSTAR_SOURCES` | `trending,following_starred` | Discovery strategies |
| `GH_AUTOSTAR_LANGUAGES` | *(all)* | Language whitelist (comma-separated) |
| `GH_AUTOSTAR_MIN_STARS` | `0` | Minimum star count |
| `GH_AUTOSTAR_MAX_STARS` | *(none)* | Maximum star count |
| `GH_AUTOSTAR_EXCLUDE_FORKS` | `false` | Skip forked repos |
| `GH_AUTOSTAR_EXCLUDE_ARCHIVED` | `true` | Skip archived repos |
| `GH_AUTOSTAR_REQUIRE_TOPICS` | *(none)* | Must have ALL these topics |
| `GH_AUTOSTAR_ANY_TOPICS` | *(none)* | Must have at least one topic |
| `GH_AUTOSTAR_EXCLUDE_OWNERS` | *(none)* | Skip these owner logins |
| `GH_AUTOSTAR_TOPIC_SEARCH_TERMS` | *(none)* | Search terms for topic_search source |
| `GH_AUTOSTAR_MANUAL_REPOS` | *(none)* | `owner/repo` slugs to always star |
| `GH_AUTOSTAR_SCHEDULER_INTERVAL_MINUTES` | `60` | Daemon run interval |
| `GH_AUTOSTAR_RUN_ON_STARTUP` | `true` | Register OS startup entry |
| `GH_AUTOSTAR_CACHE_TTL_HOURS` | `6` | Cache time-to-live |
| `GH_AUTOSTAR_LOG_LEVEL` | `INFO` | `DEBUG / INFO / WARNING / ERROR` |

### Discovery sources

- `trending` — GitHub trending repos (star-sorted search heuristic)
- `explore` — Popular recently-updated repos
- `following_starred` — Repos starred by people you follow
- `topic_search` — Repos matching `TOPIC_SEARCH_TERMS`
- `manual_list` — Explicit `MANUAL_REPOS` slugs

---

## CLI reference

```
gh-autostar --help

Commands:
  auth      Manage GitHub authentication
    login       Save a GitHub PAT
    logout      Remove the saved token
    whoami      Show authenticated user

  run         Execute one batch run immediately
    --dry-run   Discover but do not star
    --batch-size Override batch size for this run

  status      Rate limits, DB stats, startup status

  star        Star / unstar individual repos
    add <owner/repo ...>
    remove <owner/repo ...>
    check <owner/repo ...>

  history     Browse logs
    runs        Recent batch runs
    starred     Repos that were starred
    failed      Star failures
    all         All records (filterable by --status)

  cache       Manage local cache
    show        List cached repos
    prune       Remove expired entries
    clear       Clear all cache
    vacuum      SQLite VACUUM

  config      Configuration
    show        Print all settings
    set <key> <value>
    path        Print .env file location

  daemon      Background scheduler
    start       Start daemon (blocks in foreground)
    stop        Send SIGTERM to running daemon
    enable-startup   Register OS login entry
    disable-startup  Remove OS login entry
    status      Check startup registration
```

---

## Programmatic usage

```python
from gh_autostar import GitHubClient, AutoStarEngine, Database
from gh_autostar.config import Settings

settings = Settings(
    github_token="ghp_...",
    batch_size=20,
    sources=["trending", "topic_search"],
    topic_search_terms=["machine-learning", "llm"],
    min_stars=100,
    exclude_archived=True,
    languages=["python", "rust"],
)

db = Database(settings.database_path)

with GitHubClient(token=settings.github_token) as client:
    engine = AutoStarEngine(settings=settings, client=client, db=db)
    result = engine.run_batch()

print(f"Starred: {result.total_starred}")
print(f"Success rate: {result.success_rate:.0%}")
```

---

## Data storage

All data is stored in an SQLite database at:
- **Linux:** `~/.local/share/gh-autostar/autostar.db`
- **macOS:** `~/Library/Application Support/gh-autostar/autostar.db`
- **Windows:** `%APPDATA%\gh-autostar\autostar.db`

Tables: `star_records`, `batch_runs`, `repo_cache`, `starred_names_cache`, `settings_kv`

---

## Development

```bash
pip install -e ".[dev]"
pytest                          # run all tests
pytest --cov=gh_autostar        # with coverage
ruff check gh_autostar tests    # lint
mypy gh_autostar                # type-check
```

---

## License

MIT

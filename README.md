# Earnings Watch

<!-- mcp-name: io.github.benjamin-di/earnings-watch -->

Verified issuer earnings calendars and results documents through a CLI, Python API and MCP server. Models propose dates; deterministic checks require quoted evidence and consistent issuer, period and timing. A call time never silently becomes a publication time.

Calendar updates are optional. Earnings Watch produces an operation plan; your MCP client uses its external Outlook app to apply it and acknowledges confirmed writes. No Microsoft credentials or calendar SDK are required by this package.

Requires Python 3.12+. Install a release wheel with `uv tool install path/to/earnings_watch-0.1.0-py3-none-any.whl`, or develop from a source checkout:

```sh
make bootstrap
uv run earnings-watch init
uv run earnings-watch doctor
uv run earnings-watch sources import examples/sources.yaml
uv run earnings-watch watch EXAMPLE
uv run earnings-watch upcoming --format json
```

The example uses reserved domains and fictional data; replace its source URLs and issuer metadata before fetching. There is no bundled issuer universe or market-data subscription.

## Maintenance and models

Serving reads SQLite and updates personal follows/ledgers; it never fetches issuer pages or calls a model. Run maintenance separately:

```sh
uv run earnings-watch sweep --dry-run
uv run earnings-watch sweep
uv run earnings-watch sweep --docs-only
uv run earnings-watch worker
```

The default backend is `none`: it preserves existing dates and still polls official release providers. To extract new dates, install and sign into your own Claude Code or Codex CLI, then configure it in `earnings-watch.toml`:

```toml
[llm]
backends = ["claude-code", "codex", "none"]
max_calls_per_run = 20
```

Supported minimum versions are Claude Code 2.1.312 and Codex 0.164.0. Run `earnings-watch doctor --live` to check capabilities and an optional model canary. The package has no login flow. Model usage follows your CLI account's limits and terms. Missing credentials or capacity may try the next backend; a timed-out run is not repeated in the same tick.

## MCP

A local client configuration, after initialization:

```json
{"mcpServers":{"earnings":{"command":"earnings-watch","args":["serve","--stdio"]}}}
```

| Tool | Purpose |
| --- | --- |
| `earnings_upcoming` | Bounded upcoming dates with timing uncertainty |
| `earnings_get_events` | Evidence, revisions and recent history |
| `earnings_documents` | Official result-document links and verification status |
| `earnings_changes` | Cursor-based public change feed |
| `earnings_watchlist` | Your follows and registration states |
| `earnings_watch` | Follow an issuer; queue registration or resolve ambiguity |
| `earnings_unwatch` | Remove a manual follow |
| `earnings_calendar_plan` | Plan optional external calendar operations |
| `earnings_calendar_ack` | Record confirmed operations from a signed plan |
| `earnings_status` | Catalogue and maintenance status |

Prompts: `weekly_earnings_brief`, `results_brief`, `sync_to_my_outlook`. Resource: `earnings://upcoming.ics`. Exact times require verified clock and timezone evidence; otherwise calendar entries are all-day. Outlook operations never include attendees.

HTTP standalone mode uses a separate bearer token per person at `/users/<id>/mcp`. Gateway mode accepts a configured backend bearer and trusted person header at `/mcp`. These modes are separate trust boundaries; see [permissions](docs/PERMISSIONS.md).

## Docker Compose

```sh
cp earnings-watch.example.toml earnings-watch.toml
docker compose build
docker compose run --rm server init
docker compose run --rm server users add example
docker compose up -d
```

Save the returned token privately and use it only on that person's HTTP path. The server binds to loopback by default. Both images run as uid 1000; Compose drops capabilities, prevents privilege escalation and mounts the root filesystem read-only. The worker image includes pinned model CLIs; their auth volume is optional with `backends=["none"]`. Configure CLI sign-in outside the read-only worker before enabling model maintenance. Protect auth files and the data volume from other users.

Use `--config PATH`, `--person ID`, and `--json` with the CLI. Settings come from TOML and `EARNINGS_WATCH_*` environment variables. See [operations](docs/OPERATIONS.md), [architecture](docs/ARCHITECTURE.md), [providers](docs/PROVIDERS.md) and [design decisions](docs/DESIGN.md).

## Development

`make check` verifies lock drift, lint, formatting, types, dependency boundaries, offline tests and wheel/source builds. Tests use synthetic issuers and fake network/model/account boundaries. Real-model and real-page checks are separate operator gates. Read [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

See [implementation status](docs/IMPLEMENTATION.md) for completed capabilities,
release gates and the production adapter work still required before cutover.

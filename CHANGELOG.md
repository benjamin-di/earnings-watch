# Changelog

## [0.3.3] - 2026-11-04

### Fixed

- Exclude internal review records from the maintained source tree.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.3.2; no schema change.

[0.3.3]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.3.3

## [0.3.2] - 2026-11-02

### Fixed

- Publish backups only after completion so failed or interrupted copies can be retried, and create restored databases with private file permissions.
- Preserve explicit timezone offsets and fractional seconds in SEC filing acceptance timestamps.
- Represent timed calendar-feed milestones without an invalid end equal to their start; all-day events keep their next-day end.
- Use a CSV row's ISIN when its ticker contains only whitespace, preserving that holding in the import preview.
- Return HTTP 503 when the database integrity check fails.
- Refresh pending daily briefs with current facts and documents after transport-readiness delays; uncertain submissions remain immutable.
- Reject researched source keys that already identify a different issuer instead of overwriting that issuer's catalogue entry.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.3.1; no schema change.

[0.3.2]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.3.2

## [0.3.1] - 2026-11-02

### Fixed

- Publish the 0.3.0 changes; this release has no behaviour change from 0.3.0.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.2.2; calendar plans issued by 0.3.x must be re-requested after rollback.

[0.3.1]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.3.1

## [0.3.0] - 2026-11-02

### Changed

- `earnings_unwatch` adds `still_held: true` when the manual follow was removed but a holding keeps the company visible.
- An all-exchange mute shows "Unmute all exchanges" in Connections.

### Added

- Optional `codex_base_url` and `codex_key_env` route the Codex backend through an OpenAI Responses-compatible endpoint while keeping `--ignore-user-config`; requests retry at most twice, and an endpoint that is unreachable or failing before any output falls back to the next backend within seconds; a run that already produced output is not repeated.
- Claude Code receives `CLAUDE_CODE_MAX_RETRIES` from the environment.

### Fixed

- Do not read a numeric date such as 12.05.2027 as a publication time.
- Bind a quoted date without a year to its reporting period, and honour an explicit year written after a dotted month (`31 Jul. 2025`).
- Map "as at" period ends to the configured fiscal year, matching "as of".
- Poll HKEXnews only for Hong Kong listings; numeric tickers on other exchanges use their issuer pages.
- Stop model-backed document research once a release is verified.
- Fail once on a redirect without a new location instead of repeating the request.
- Accept a calendar acknowledgement when only unrelated events changed, avoiding duplicate external entries.
- Let Unmute clear a mute set for all exchanges; removing a watchlist row no longer mutes a separate holding.
- Skip CSV rows without a ticker or ISIN; show plain messages for non-UTF-8 uploads and unknown time zones.
- Apply the model timeout while sending large prompts, fall back when a CLI refuses before reading, end leftover model child processes, and retry another backend only for explicit sign-in or account-limit refusals.
- `health` and `doctor` exit 1 when the database integrity check fails.
- `upcoming` accepts `--cursor` and `--ticker`; a missing or unavailable database (exit 3) and malformed source files (exit 1) report JSON errors instead of tracebacks.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.2.2; calendar plans issued by 0.3.0 must be re-requested after rollback (handles last 30 minutes).

[0.3.0]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.3.0

## [0.2.2] - 2026-10-31

### Fixed

- Explain ambiguous watchlist tickers in Connections and require an exchange before saving.
- Accept CSV identity headers regardless of case or surrounding spaces; reject duplicate identity columns before importing holdings.
- Accept single-column ticker/ISIN CSV files and identity columns after other columns; preserve preview and per-person replacement boundaries.
- Preserve plaintext holdings imports across CR, LF and CRLF line endings.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.2.1.

[0.2.2]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.2.2

## [0.2.1] - 2026-10-31

### Fixed

- Align MCP Registry metadata with the released package version.
- Permit the public Outlook subscription host in sanitized release exports so calendar setup ships with the Connections website.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.2.0.

[0.2.1]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.2.1

## [0.2.0] - 2026-10-31

### Changed

- Personalize default event, change and document reads from each person's holdings and watchlist, with localized summaries, certainty and freshness.
- Replace the `weekly_earnings_brief` prompt with `earnings_briefing` and retain `results_brief` and `sync_to_my_outlook`.
- Require explicit event selection for new Outlook plan/ack operations; preserve previously delegated entries when unfollowing.

### Added

- Add an optional Connections website with PKCE sign-in, private holdings previews, watchlists, persistent mutes and notification settings.
- Add revocable private calendar subscriptions and copy/subscribe controls.
- Add results-out mail, useful local daily briefs and coalesced urgent timing changes through an external transport with durable uncertain-submission recovery.
- Add exact-subject directory admission, stopped-writer external-effect baselines and database-bound cursors that invalidate after restoration.

### Upgrade

- Action required: stop writers, back up the database and signing key, then run `earnings-watch init` to upgrade schema 1/2 to 3. Configure optional Connections, directory and transport settings before enabling them; reconnect clients to refresh the prompt list.
- Rollback: schema 3 is incompatible with 0.1.0. Restore the separately rehearsed database/signing-key backup with all writers stopped, or fix forward; an image rollback alone does not restore data.

## 0.1.0

Initial standalone CLI, Python API and MCP server. Shared SQLite facts with personal follows, delegated calendar plans and durable delivery state; deterministic date/timing verification; official release polling, document links and opt-in downloads; standalone and gateway HTTP authentication; separate serving and maintenance images.

Candidate audit fixes preserve revisions and download receipts across catalogue
retirement, cover the full release-polling window, keep accepted documents after
partial provider failures, align date boundaries to UTC, treat disabled models
as intentional, and harden interrupted HTTP requests and download integrity.
Existing schema-1 candidates upgrade explicitly through `init`; see operations.

- Preserve legacy polling, document verification and reviewed overrides during
  migration; block external effects until explicit reconciliation, and export
  current-state compatibility projections with retained rollback evidence.
- Bind concurrent image builds to their own builders so one completed job cannot
  stop another job's qualification or release build.

[0.2.0]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.2.0

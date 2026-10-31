# Changelog

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

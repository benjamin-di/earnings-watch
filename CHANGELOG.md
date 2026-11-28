# Changelog

## [0.12.0] - 2026-11-28

### Added

- Targeted failed-issuer retry on retry sweeps (with 4-hour backoff after repeated failed retries) so a single failing issuer no longer re-sweeps the entire catalogue every hour.
- Sweep-level transport circuit breaker that aborts early after 3 consecutive hub/transport failures (`NotStarted`, `model run failed`, `model run timed out`) and preserves full-sweep retry once connectivity returns.
- `stale_error` coverage warning when an issuer has `>= 3` consecutive fetch failures and no successful check in >72 hours.
- Layer-1 discovery fallback on failed `plain` fetches and automatic promotion from `plain` to `websearch` after repeated HTTP 403/202 WAF blocks.

### Fixed

- Allow standard 1-turn JSON validation repair (`allow_repair=True`) on `repair_rejected()`, skip repair calls for claims already covered by verified held events, and catch `repair_rejected()` failures so already-verified events are never discarded.

### Upgrade

- Action required: none.
- Rollback: compatible with `0.11.0`; no schema change.

## [0.11.0] - 2026-11-28

### Added

- Optional `[connections] digests_tab = false` and `digests_external_url` settings to hide the `Digests & alerts` tab and show an inline timezone selector + external link on `Upcoming` (default `digests_tab = true` preserves standalone behavior unchanged).
- Optional `pref_overrides` parameter on `notify.notices()` and `notify.prepare()` for host runtimes managing subscriptions externally.

### Fixed

- Fall back from MarkitDigital's 5-item-capped ASX JSON feed to ASX's full-year HTML announcements table (`www.asx.com.au`) when no historical results dates are returned, restoring cadence projection for ASX issuers with frequent routine filings.
- Filter already-covered rejected claims (exact-date matches against verified events, or past-dated claims already covered by a past event) out of `fetch_state.rejected` and sweep errors so redundant unverified claims do not raise false-positive coverage notices or mark sweeps failed.
- Scope web hub coverage notices to the Upcoming and Companies views rather than Digests & alerts and Calendar.

### Upgrade

- Action required: None.
- Rollback: Compatible with `0.10.1` with no schema changes.

## [0.10.1] - 2026-11-27

### Fixed

- Raise uvicorn HTTP keep-alive timeout to 65 seconds so reverse proxies can reuse idle upstream connections across 60-second windows without hitting the 5-second default timeout.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.10.0.

## [0.10.0] - 2026-11-27

### Changed

- Serve the Earnings Watch web home at canonical root `/` using `signin-window 0.15.0` and permanently redirect legacy `GET /connections` and `GET /connections/` (`308`, `Cache-Control: no-store`, preserving query parameters) to `/` while keeping OIDC and POST action routes under `/connections/*`.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.9.0; no schema change.

## [0.9.0] - 2026-11-27

### Added

- Add a lightweight ASX MarkitDigital public announcements discovery adapter (`providers/asx.py`, `providers/definitions/asx.toml`) for `XASX` issuers, converting announcement timestamps in `Australia/Sydney` and adding `markitdigital.com` to `_SHARED_PLATFORM_SUFFIXES`.
- Preserve `[Est.]` cadence projections when a sweep extraction fails, using both existing history and Layer 1 `historical_dates`, while persisting allowlist-sanitized `error_detail` and preserving `last_ok`/`last_agent_audit` in `fetch_state`.

### Fixed

- Align the plain-fetch extraction prompt with all supported corporate milestones (`earnings`, `earnings-call`, `agm`, `cmd`, `dividend`, `conference`, `trading-update`).
- Canonicalize AGM periods to the meeting date's calendar year, deduplicate same-day non-earnings aliases in `reconcile()` while preserving distinct subtypes (AGM vs EGM, regular vs special dividends, and `ex`/`record`/`pay` milestones), and remove superseded same-day duplicates in `Catalogue.commit_issuer()` even on non-authoritative sweeps.
- Widen `Source.hkex_stock_id` validation to 1–12 digits for 10-digit HKEX IPO stock IDs.
- Enable Q4 Inc discovery on `websearch` `/events-and-presentations` issuer IR URLs without `default.aspx` and short-circuit non-Q4 hosts when `Event.svc` does not return a `GetEventListResult` list.
- Suppress unverified `timing_candidate` rejections on past events (`date < today`) and clear stale single-source row-disagreement `timing_issue` flags on clean `ir-confirmed` re-verification.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.8.1, 0.8.0, and 0.7.0; no schema change.

## [0.8.1] - 2026-11-27

### Fixed

- Fall back to the configured `browser` reader in `registration.register_pending()` and `heal.repair()` when an official IR page returns HTTP 403 or 5xx, wiring `Maintenance.browser` into both paths.
- Accept plain-text `navigate` responses in `McpBrowserReader.read()` instead of requiring a JSON object.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.8.0; no schema change.

## [0.8.0] - 2026-11-27

### Changed

- Upgrade `signin-window[oidc]` from `0.4.17` to `>=0.14.0,<0.15`.
- Align the Connections web UI (Upcoming, Companies, Digests & alerts, Brief preview, Calendar, holdings import review and error pages) with the shared suite design: light and dark themes, elevated cards, ink primary buttons, calmer badges and callouts, collapsible coverage notices, sentence-case copy and a 390px mobile layout. Routes, forms and authorization are unchanged.
- Adopt `issuer-sources` v0.1.0, provenance CDN verification and library-targets v2.

### Added

- Support the optional `SIGNIN_WINDOW_SUITE` directory through `signin-window` 0.14.0: signed-in pages show a top bar with a group-filtered **Apps** switcher and account chip. Signed-out, refused and error pages never show it.

### Upgrade

- Action required: none. Optionally set `SIGNIN_WINDOW_SUITE` to enable the Apps switcher (see `docs/CONNECTIONS.md`). A sign-in that is mid-flight at the moment of upgrade is refused once and must be started again.
- Rollback: compatible with 0.7.0; existing browser sessions survive upgrade and rollback; no schema change.

## [0.7.0] - 2026-11-27

### Added

- Expand `EventType` with `conference` and `trading-update`, add `DatePrecision` (`exact`, `week_of`, `month`, `tentative`), and disambiguate recurring/multi-milestone dividend IDs.
- Emit `dividend`, `conference`, and `trading-update` in calendar plans and `.ics` feeds, prefix `expected` events with `[Est.] ` across UI and ICS, and display `week_of`/`month`/`tentative` date precision honestly.
- Add deterministic official discovery adapters: Q4 Inc JSON events and press releases (`providers/q4_ir.py`), HKEXnews board-meeting/AGM/dividend notices with bounded `pypdf` PDF extraction (`providers/hkexnews.py`, `providers/pdf.py`), SEC EDGAR `DEF 14A` and historical 8-K/6-K discovery (`providers/sec_edgar.py`), and `.ics`/JSON-LD/PDF support in `providers/generic_ir.py`.
- Add pure-engine historical cadence projection (`engine/cadence.py`) so active issuers without a confirmed upcoming earnings date maintain an `[Est.]` `expected` placeholder that automatically promotes on issuer confirmation and retires cleanly if unconfirmed past its date.
- Upgrade the IR Analyst research loop with hardened registrable apex-domain + official exchange scoping (`model/urls.py`, `transport.py`), 10-turn multi-surface checklist prompts with bounded Layer 1 official documents, 1-turn verification-rejection repair (`research.repair_rejected()`), rolling weekly deep audits, and `coverage_warnings` in `PersonService.status()` and Connections UI (respecting `include_holdings`).

### Fixed

- Fix combined results-and-conference-call classification in `engine/timing.py` and `engine/verify.py` so combined calendar rows remain `ir-confirmed` across multi-sweep reconciliations, and normalize comma-formatted US dates in `"Week of ..."` precision inference.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.6.3, 0.6.2, 0.6.1, and 0.6.0; no schema change.

[0.7.0]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.7.0

## [0.6.3] - 2026-11-27

### Fixed

- Skip direct `IdentityDirectory` construction inside `notify.prepare()` when `directory_key_env` is unset in role-separated `worker` and `notifier` containers that share a config with `directory_url` set while intentionally withholding the directory API key; keep direct directory enforcement whenever the key is configured or a `directory` instance is passed.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.6.2, 0.6.1, 0.6.0, 0.5.3, 0.5.2, 0.5.1, 0.5.0, 0.4.4, 0.4.3, 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.6.3]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.6.3

## [0.6.2] - 2026-11-27

### Security

- Enforce `portfolio_admitted` consistently across `/mcp`, `PersonService`, `calendar.plan()`, `PersonService.unwatch()`, `/connections/unwatch`, `notify.prepare()`, `outbox.deliver()`, and CLI `notify` so revoked or unadmitted accounts cannot read, probe, mute, or receive emails for `holdings:*` follows.
- Sanitize event and document `<a href>` links in Connections UI with `_safe_url()` and validate `Event.source_url` / `ResultsLink.url` via `public_url()` (also rejecting empty userinfo and multicast IP literals/DNS responses).
- Add `"purpose"` domain separation to signed `calendar.plan` and `interests.preview` HMAC handles, validate/normalize and cap `accounts.mute()` rows, and return `401`/`403` (instead of `503`/`409`) for invalid subjects and revoked accounts while reserving `503` for upstream directory failures.
- Disable keep-alive connection pooling across redirect hops in `Transport.request()` and reset `User-Agent` per hop so cross-host CDN redirects perform fresh TLS hostname verification and never leak `sec_contact`.

### Fixed

- Handle `list[str]` as well as comma-separated `str` for SEC EDGAR `filings.recent.items`, and forward `sec_contact` across CLI downloads, registration, URL healing, and research `scope.json`.
- Throttle `polling.py` LLM research fallback (`offset >= 0`, 6-hour cooldown, attempt cap) and isolate `results_url` fetch failures; fix `CliRunner` `"result": null` handling and repair tool preservation; fix `worker.sweep_due()` so post-midnight retries do not skip the next daily `23:10 UTC` sweep.
- Anchor `evidence_verifies()` on alphanumeric boundaries, support optional commas and US/non-US slash dates in `dates_in_evidence()`, wire `check_timing_candidates()` into `Maintenance.sweep()`, require explicit `removal_overrides` for `MAX_AUTO_REMOVALS` holds, fix `period_consistent()` `H1`/`H2` <-> `Q2`/`Q4` equivalence, `_AS_AT_RE` day/quarter parsing, `with_legacy_evidence()` `PAST` preservation, `timing_search.py` `UTC±N`/`GMT±N` offset rejection, and `results_from_calls` retention.
- Persist canonical `sources.exchange` in `sync_holdings()`, propagate `(ticker, exchange)` updates per-row in `Catalogue.put_source()`, fix `PersonalStore.unwatch(..., exchange="")` wildcard deletion, align `legacy.reconcile()` / `notify.event_notices()` baseline keys, use a 24-hour UTC follow-up window in `notify.notices()`, map `DocRoleLiteral` aliases in `PersonService.documents()`, normalize exchanges in `interests._match_sources()`, strip whitespace in `settings.load()` list env vars, and offload blocking SQLite calls in async HTTP/Connections handlers.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.6.1, 0.6.0, 0.5.3, 0.5.2, 0.5.1, 0.5.0, 0.4.4, 0.4.3, 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.6.2]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.6.2

## [0.6.1] - 2026-11-27

### Fixed

- Add `METHOD:PUBLISH`, `NAME:Earnings Watch`, and `X-WR-CALNAME:Earnings Watch` to rendered `.ics` feeds plus `Content-Disposition: inline; filename="earnings-watch.ics"` on `/calendar/{token}.ics` so calendar apps display `Earnings Watch` instead of the raw 64-character feed token.
- Remove the broken `Add to Outlook` web deep link (`outlook.live.com` fails for Microsoft 365 accounts and `jsmvvmdeeplink` drops `rru=addsubscription` across auth redirects), leaving the reliable copyable `https://` link (with a short Outlook **Add calendar → Subscribe from web** hint on Windows) and the `webcal://` button on macOS/iOS/other platforms.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.6.0, 0.5.3, 0.5.2, 0.5.1, 0.5.0, 0.4.4, 0.4.3, 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.6.1]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.6.1

## [0.6.0] - 2026-11-27

### Added

- Owned `1080px` responsive Earnings Watch web shell (`ui.hub_page`) with external `/connections/assets/{app.css,app.js}` under strict `script-src 'self'; style-src 'self'` CSP, preserving `BrowserAuth` for OIDC, cookies, and origin verification.
- Unified Companies list merging overlapping portfolio holdings and watchlist entries by `(ticker, exchange)` into responsive card rows with instant client-side search and `All / Portfolio Holdings / Watchlist / Muted` filtering.
- Progressive-enhancement JSON mutations (`Accept: application/json`) that sync every `input[name="revision"]` in the DOM, preserve scroll position, show live status updates and inline errors, and retain full no-JS `303`/`409` HTML form fallbacks.

### Fixed

- Clean up event titles and summaries (`2027 Dividend`, `2026 AGM`, `Q4 Results`) to eliminate redundant period/type repetition.
- Stop auto-clearing `seen_seq` on `Upcoming` GET so `Changed` badges persist across refreshes until explicitly acknowledged via `Mark changes seen`.
- Fix mobile (`<= 640px`) company row wrapping, unify `Digests & Alerts` into a single `Save preferences` form with a `00:00`–`23:00` hour selector, and confirm calendar link copies with `"Link copied"`.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.3, 0.5.2, 0.5.1, 0.5.0, 0.4.4, 0.4.3, 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.6.0]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.6.0

## [0.5.3] - 2026-11-26

### Fixed

- Omit the `webcal://` "Subscribe in calendar app" button on Windows clients (where New Outlook's protocol handler strips `webcal:` to `//host/...` and rejects it as an invalid URL), and show a clear New Outlook for Windows hint directing users to "Add to Outlook" or the copyable `https://` link while keeping `webcal://` on macOS, iOS, and other platforms.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.2, 0.5.1, 0.5.0, 0.4.4, 0.4.3, 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.5.3]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.5.3

## [0.5.2] - 2026-11-26

### Added

- Enrich notification outbox payloads (`results`, `urgent`, `brief`, `weekly_brief`) with user `timezone`, `has_holdings`, and per-event `why_followed` and `issuer_name` so downstream mail renderers format local times and data-driven holding badges accurately.
- Add non-mutating `GET /connections/?view=preview` (with upfront `include_holdings` gating) and a per-user `Recent Earnings Deliveries` table on the Digests tab (prioritizing active `pending`/`unknown` rows and excluding legacy/migration rows).

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.1, 0.5.0, 0.4.4, 0.4.3, 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.5.2]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.5.2

## [0.5.1] - 2026-11-26

### Fixed

- Fix the Calendar tab's "Add to Outlook" link to use Outlook's `rru=addsubscription` web subscribe deep link and add a `webcal://` calendar app subscription button.
- Replace the Digests & Alerts timezone `<input list="timezones">` with a full IANA `<select>` dropdown (Common + All timezones) so changing the timezone no longer filters to only the currently saved value.
- Improve new/untracked company onboarding on the Companies tab: normalize common exchange aliases (`NASDAQ`, `NYSE`, `HKEX`, `LSE`, `TSE`, etc.) to ISO 10383 MICs, add a "Request new company" popover supporting optional company name and official IR URL hints, show clear guidance when an untracked company name is entered without a ticker or ISIN, and preserve the active tab on 409 error return links.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.5.0, 0.4.4, 0.4.3, 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.5.1]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.5.1

## [0.5.0] - 2026-11-26

### Added

- Unified `1080px` zero-JS 4-tab Company Hub Web UI (`Upcoming`, `Companies`, `Digests & Alerts`, `Calendar`) using native HTML Popover API with strict `portfolio_admitted` holdings isolation across UI, CSV import, and `.ics` feeds.
- Read-only bearer-authenticated `GET /internal/targets` and `GET /internal/library-targets` endpoints, plus opt-in `[integrations.news_digest]` user preferences stored in `profiles.settings` (zero schema migration, `PRAGMA user_version=3`).
- Pure-stdlib HTML/text email renderer (`render.py`), TLS-only SMTP delivery (`notify --channel smtp --all`), and `weekly_brief` notifications.
- Multi-user onboarding improvements, symbol/exchange normalization (`.DE`, `BRK.B`, ISIN), partial CSV import, and `Source` `hkex_stock_id`/`lei` fields.

### Fixed

- Recognize combined results-and-call rows in `timing.call_only`.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.4.4, 0.4.3, 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.5.0]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.5.0

## [0.4.4] - 2026-11-26

### Fixed

- Cap automatic removals during an authoritative sweep to 3 per pass before calling `commit_issuer()` so multi-event `removal-held` expirations drain cleanly across consecutive sweeps instead of permanently wedging the issuer on `automatic removal cap exceeded`.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.4.3, 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.4.4]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.4.4

## [0.4.3] - 2026-11-26

### Fixed

- Add a 60-minute cooldown before `worker.sweep_due()` retries a completed failed sweep (`exit != 0`) so transient source failures run `docs_only` ticks instead of re-sweeping every 10 minutes.
- Keep `claims_rejected` recorded in run diagnostics and `fetch_state` without failing the entire sweep's exit code (`exit=3`) when no source fetch or runner error occurred.
- Instruct extraction and research prompts to use IANA timezone names (such as `America/New_York` or `Europe/London`) for `release_tz` rather than abbreviations.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.4.2, 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.4.3]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.4.3

## [0.4.2] - 2026-11-25

### Fixed

- Prevent `notify.notices()` from enqueueing a fresh `:initial` results notification when an event already has a verified release document recorded at or before the subscriber's baseline cutoff, and compare timestamps as UTC instants rather than raw strings.
- Seed `suppressed_by_migration` markers in `archive_effects()` for legacy events with `results_emailed_at` so later document discovery cannot re-send pre-migration results without claiming a native `sent` delivery.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.4.1, 0.4.0, and 0.3.3; no schema change.

[0.4.2]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.4.2

## [0.4.1] - 2026-11-23

### Fixed

- Pass `--platform "linux/$ARCH"` to `docker image inspect` in the release smoke job so multi-arch `arm64` OCI label verification reads the pulled platform manifest on Docker Engine 29+.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.4.0 and 0.3.3; no schema change.

[0.4.1]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.4.1

## [0.4.0] - 2026-11-23

### Added

- Show the producing build on Connections pages and in native MCP server information.
- Attribute retained application diagnostics to the packaged build and process while preserving command results and protocol output.

### Fixed

- Remove internal side-effect prefixes (`R0.`/`R1.`) from MCP tool descriptions and add explicit tool titles, parameter descriptions, enum literals, and lean input schemas across all ten MCP tools.
- Return MCP tool errors with `isError: true` while preserving the closed JSON error envelope.
- Retain safe CLI guidance and failure stack locations, and restore source stamps after package qualification.
- Preserve native Rich render hooks, subclass print options and enclosing host capture during diagnostic attribution.

### Upgrade

- Action required: none.
- Rollback: compatible with 0.3.3; no schema change.

[0.4.0]: https://github.com/benjamin-di/earnings-watch/releases/tag/v0.4.0

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

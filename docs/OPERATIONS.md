# Operations

Initialize once with `earnings-watch init`. The default database is `$XDG_DATA_HOME/earnings-watch/earnings.db`, falling back to `~/.local/share/earnings-watch/earnings.db`. Set `EARNINGS_WATCH_DATA_DIR` or `[paths] data_dir` to use a persistent volume. `calendar.key` sits beside it. Do not store real state in a source checkout.

`doctor` checks database integrity and configured CLI versions/flags; `doctor --live` also invokes a model canary. `health` is an offline database check. Health does not establish that a provider or account is currently usable. Exit codes are 0 for success, 1 for invalid/error and 3 for skipped/degraded/unavailable work.

Run one worker per data directory. A shared maintenance lock prevents overlapping sweeps, document polls, imports and restores. The UTC worker runs a daily sweep from 23:10 and a documents pass every ten minutes. Failed daily sweeps take priority and retry on bounded ticks, including after midnight and process restarts. Pause with `freeze`; resume with `unfreeze`. `preflight` and `docs-guard` are cheap operator probes; execution rechecks its gates.

Upcoming queries, reminder dates and document guards use the same UTC day as the
worker, regardless of the host timezone. With the model backend disabled, a sweep
skips registration and calendar extraction while retaining official release/link
polling. Disabled models do not make the daily sweep fail.

Release polling covers the day before an event through seven days afterward.
Its lifetime cap covers the full configured cadence (650 attempts per event
date); redating an event resets its attempt window. Per-host request budgets and
backoff still apply. Successfully discovered official documents and filing cursors
are saved even if a later issuer-page fetch fails.

Use `sources validate FILE`, then `sources import FILE`. `watch TICKER --exchange MIC` creates a manual follow. Unknown issuers queue bounded research registration. `holdings sync --feed NAME FILE` atomically replaces one feed's entries and preserves manual follows; its YAML list must reference imported, unambiguous sources. `unwatch` removes a manual follow or mutes a holding; a mute survives the next holdings refresh.

`overrides set ID --kind remove` and `overrides set ID --kind timing_selection --payload JSON` record operator decisions. Timing selections require the candidate fingerprint, review date and reason. `overrides import FILE` accepts a list of `event_id`, `kind`, `payload`; `clear` removes one decision. A decision does not invent evidence.

Notifications are prepared durably but are not sent by the default worker. Invoke `notify --channel stdout` or `notify --channel webhook --url HTTPS_URL` explicitly. The three active kinds are results-out, useful local daily briefs and coalesced urgent timing changes. Existing historical delivery records are retained for reconciliation. Inspect without sending with `notify --preview` or `notify --inspect`. Resolve an unknown with `notify --resolve KEY --receipt FILE`, using transport evidence, person, key, current attempt and payload hash; never guess that a timed-out submission did not send. Delivery keys are stable. The outbox marks an attempt unknown before contacting a channel; an unknown outcome is never replayed automatically. Reconcile uncertain deliveries externally before migrating or replaying them. SMTP and direct Outlook writing remain external integrations. See [Connections setup](CONNECTIONS.md) for browser identity, feeds and settings.

Use `backup NEW_PATH` for a consistent SQLite backup. Maintenance retains seven daily backups. Keep a separate protected copy and the signing key. To restore, stop serving, `freeze`, run `restore BACKUP_PATH`, validate with `doctor`, then restart and unfreeze. Do not treat rolling back an image as a state rollback. Newer schema versions are refused.

Schema 2 decouples historical personal download receipts from the current public
document catalogue. Removing an event retires its catalogue documents but keeps
the user's files and receipts. A new download still requires a current catalogue
document; deleting a person removes that person's receipts.

To upgrade a schema-1 candidate, freeze it and take a database backup with the old
version, preserve `calendar.key`, and stop the server and worker. Rehearse on a
copy first. Run `earnings-watch init` with the new version against the stopped
data directory, then run `doctor`, restart and unfreeze. The upgrade is atomic and
idempotent; ordinary reads and worker startup never migrate a database. An older
binary cannot read schema 2. Rollback requires the older binary and its matching
database backup, with the original signing key.

Legacy migration must use a stopped-state copy containing `ir-sources.yaml`, `events.yaml` and optionally `earnings-watch-mail.json`. Import into an empty database with `import --legacy DIR`. It splits facts, polling, documents and calendar state atomically and retains original projections in an unknown migration receipt. `export events --legacy`, `export sources --legacy` and `export mail --legacy` support rehearsal. Do not cut over a live installation until consumer parity, calendar/mail reconciliation, backup restoration and the quiet window have passed.

Explicit CLI downloads record a per-person receipt and cache public content by its
full SHA256 hash. A content change gets a new output file; earlier user downloads
are preserved. The internal blob cache evicts least recently used content within
`cache_max_mb` and removes abandoned cache entries on the next download. The small
conditional-response cache has the same byte ceiling and an additional 64-entry
limit. Cache files are disposable; include the database and desired user downloads
in backups. Python callers that record downloads must use the maintenance lock.

`make conformance` requires Node/npm and runs the pinned official MCP conformance
CLI against a disposable loopback HTTP server. Its committed baseline covers
initialization, ping, tool/resource/prompt discovery and DNS rebinding refusal.
The fixture supplies a synthetic gateway identity; real authentication refusals
and application tool calls are covered by the regular wire tests. Scenarios that
require the suite's named kitchen-sink tools are outside this application's
surface. Results are written under `.artifacts/conformance/`.

Public publication uses the tested wheel/source assets attached to the GitHub
release. The public `Release` workflow leaves PyPI disabled until
`PYPI_PUBLISH=true` and the PyPI trusted publisher is registered for repository
[earnings-watch](https://github.com/benjamin-di/earnings-watch), workflow
`release.yml`, environment `pypi`.
`MCP_REGISTRY_PUBLISH=true` enables the subsequent Registry job only after the
version is available on PyPI. Registry login uses GitHub OIDC and a verified
publisher binary. Re-dispatch the workflow on the release tag to finish a
publication after one-time account setup; no new images or distributions are
built when that GitHub release already exists.


Legacy imports seed the notification cursor and block new notification enqueue,
delivery and calendar plans for the migrating person. Imported calendar IDs and
fingerprints stay in the retained snapshot until externally verified; no event ID
is invented as a provider ID. Documents without verification timestamps remain
unverified, and release attempts/cursors keep their original budget and cadence.
Optional `events-overrides.yaml` removals and timing selections migrate with the
same transaction and identity mapping.

Use `review-legacy --json` to inspect desired calendar fingerprints, imported
remote markers and retained pending/mail evidence while effects remain blocked.
Protect that personal output. After reviewing real calendar/mail effects, run
`reconcile-legacy RECEIPT.json`.
The receipt requires `evidence` (a review reference), `notifications_reviewed: true`,
and `calendar` entries containing `key`, the **current desired** `fingerprint` and
confirmed `provider_id`. Every previously synced calendar key must be confirmed
or explicitly included in `calendar_absent` after an external absence check.
For example, a key has the form `earnings-watch:earnings:example:2026Q4`.
The command validates the complete receipt atomically. It records existing event
notices as a reviewed baseline and advances the change cursor so reconciliation
does not replay imported reminders/documents. New events or changed documents
retain normal delivery behavior. A failed review leaves all effects blocked.
This command records an external review; it cannot perform that review itself.

Legacy exports reflect current sources, documents, polling state and event
retirement. Original mail markers and pending effects remain preserved. Treat
these as read-only compatibility projections. Roll back using the original
stopped snapshot and matching legacy image, rather than resuming the old writer
against new calendar fingerprints or partially reconciled effects.

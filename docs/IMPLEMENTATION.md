# Implementation and qualification

The standalone core includes Connections and personal subscriptions. Release
publication and production migration are separate acceptance gates; installing a
published package does not activate an existing installation's replacement.

| Area | Implemented behavior |
| --- | --- |
| Core | Shared SQLite issuer facts, evidence verification, revision/change feed, person-scoped follows and quotas |
| Interfaces | CLI, Python service, stdio MCP, authenticated standalone and gateway HTTP MCP |
| Connections | Browser sign-in, private holdings previews, watchlists, persistent mutes, directory email and revocable calendar subscriptions |
| Maintenance | Bounded model extraction and research, registration, source repair, provider selection, restart-safe UTC worker |
| Documents | SEC/HKEX and issuer-page polling, verified links, opt-in downloads, content-addressed cache and personal receipts |
| Calendar | Optional signed plan/ack and ICS; the client's external Outlook app applies writes |
| Notifications | Results-out, useful daily briefs and urgent changes; explicit transport, no automatic uncertain-effect replay |
| Operations | Backup/restore, freeze, health/doctor, legacy import/export, separate server/worker images |

These capabilities are implemented in the source. Their offline tests use fake
network, model, identity and mail boundaries; they do not establish that an
installation's accounts, provider pages or transport are usable. Connections is
optional, and its identity directory and mail transport are configured externally.

Legacy migration blocks notification delivery and calendar planning until an
external review accounts for imported effects. Original snapshots remain rollback
evidence; exports project the current catalogue without exposing storage fields.

Lifecycle regressions cover monotonic revisions after
removal/reintroduction, personal download retention, schema upgrades, UTC date
boundaries, disabled models, interrupted HTTP requests, download integrity and
the complete release-polling window. Accepted official documents survive a later
issuer-page failure; the failure remains visible to maintenance.
Registration rejects a proposed source key belonging to another issuer. SEC
timestamps retain explicit offsets, calendar feeds represent timed milestones
without an invented duration, and unsent briefs refresh before submission.

## Release qualification

Apply these checks to each release. Historical test counts and a published
version do not replace validation of a new source tree. Installation-specific
delivery status belongs in the operator's private deployment records.

- Qualify the exact source through CI, including both Docker targets on both
  supported architectures and the installed model binaries.
- Complete live acceptance for source repair and the optional browser reader.
  Offline mock coverage does not establish live account or provider availability.
- Review the exact public source tree and its privacy checks, then verify the
  immutable public tag, release, distributions, images, PyPI and MCP Registry
  publication. Source-home changes follow the [source contract](SOURCE.md).

## Required before replacing an existing installation

- Qualify the private production adapter against each installation. Current-state
  compatibility projections, receipt-gated effect reconciliation and normalized
  document/polling/override migration are implemented.
- Rehearse with a stopped snapshot; compare events, documents, personal follows,
  calendar ledgers and uncertain notification outcomes against each consumer.
- Prove database/signing-key restoration, arrange the quiet window, and retain
  state rollback evidence separately from the image rollback.
- Reconcile uncertain deliveries externally before enabling notification delivery.

Schema 3 preserves personal profiles and subscription state as well as personal
download receipts when their public events/documents are retired. Existing
databases with older schemas require an explicit stopped upgrade; see
[operations](OPERATIONS.md). No live data is migrated by installing a wheel.

## External integrations

An in-package SMTP sender and a direct Outlook writer are not implemented.
Notifications use an explicit external transport. The optional delegated calendar
workflow already supports Outlook through the client's app and requires no
Microsoft credentials in this package. Hosted routing, mail transport and
production migration belong to each installation.

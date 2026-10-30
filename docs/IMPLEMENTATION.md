# Implementation and remaining work

The standalone 0.1.0 source is implemented. Publication and production migration
are separate acceptance gates; a passing local check does not establish either.

| Area | Implemented behavior |
| --- | --- |
| Core | Shared SQLite issuer facts, evidence verification, revision/change feed, person-scoped follows and quotas |
| Interfaces | CLI, Python service, stdio MCP, authenticated standalone and gateway HTTP MCP |
| Maintenance | Bounded model extraction and research, registration, source repair, provider selection, restart-safe UTC worker |
| Documents | SEC/HKEX and issuer-page polling, verified links, opt-in downloads, content-addressed cache and personal receipts |
| Calendar | Optional signed plan/ack and ICS; the client's external Outlook app applies writes |
| Notifications | Seven durable outbox projections, explicit stdout/webhook delivery, no automatic uncertain-effect replay |
| Operations | Backup/restore, freeze, health/doctor, legacy import/export, separate server/worker images |

Legacy migration blocks notification delivery and calendar planning until an
external review accounts for imported effects. Original snapshots remain rollback
evidence; exports project the current catalogue without exposing storage fields.

The candidate audit added lifecycle regressions for monotonic revisions after
removal/reintroduction, personal download retention, schema upgrades, UTC date
boundaries, disabled models, interrupted HTTP requests, download integrity and
the complete release-polling window. Accepted official documents survive a later
issuer-page failure; the failure remains visible to maintenance.

## Required before release

- Qualify the exact source through CI, including both Docker targets on both
  supported architectures and the installed model binaries.
- Complete live acceptance for source repair and the optional browser reader.
  Offline mock coverage does not establish live account or provider availability.
- Review the actual tagged public export and its privacy checks, then verify the
  public release, distributions, images, PyPI and MCP Registry publication.

## Required before replacing an existing installation

- Qualify the private production adapter against each installation. Current-state
  compatibility projections, receipt-gated effect reconciliation and normalized
  document/polling/override migration are implemented.
- Rehearse with a stopped snapshot; compare events, documents, personal follows,
  calendar ledgers and uncertain notification outcomes against each consumer.
- Prove database/signing-key restoration, arrange the quiet window, and retain
  state rollback evidence separately from the image rollback.
- Reconcile uncertain deliveries externally before enabling notification delivery.

Schema 2 preserves personal download receipts when their public events/documents
are retired. Existing candidate databases require an explicit stopped upgrade;
see [operations](OPERATIONS.md). No live data is migrated by installing a wheel.

## Deferred integrations

SMTP, a direct Outlook writer and a fleet-hosted MCP endpoint remain deferred.
The current optional calendar workflow uses the MCP client's external Outlook
app and requires no Microsoft credentials in this package.

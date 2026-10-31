# Implementation and remaining work

The 0.2.0 candidate adds Connections and personal subscriptions to the standalone core. Publication and production migration
are separate acceptance gates; a passing local check does not establish either.

| Area | Implemented behavior |
| --- | --- |
| Core | Shared SQLite issuer facts, evidence verification, revision/change feed, person-scoped follows and quotas |
| Interfaces | CLI, Python service, stdio MCP, authenticated standalone and gateway HTTP MCP |
| Maintenance | Bounded model extraction and research, registration, source repair, provider selection, restart-safe UTC worker |
| Documents | SEC/HKEX and issuer-page polling, verified links, opt-in downloads, content-addressed cache and personal receipts |
| Calendar | Optional signed plan/ack and ICS; the client's external Outlook app applies writes |
| Notifications | Results-out, useful daily briefs and urgent changes; explicit transport, no automatic uncertain-effect replay |
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

The Connections and subscription implementation at tree
`94f3729db63299b81095f64f4963339edd9ab2a2` passed an independent sensitive
re-review on 2026-10-31: 104 focused tests and 21 adversarial probes. The
complete local gate passed 294 tests, static checks and distribution builds.
This is source validation; production migration and live acceptance remain
separate requirements below.

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

Schema 3 preserves personal profiles and subscription state as well as personal download receipts when their public events/documents
are retired. Existing candidate databases require an explicit stopped upgrade;
see [operations](OPERATIONS.md). No live data is migrated by installing a wheel.

## Deferred integrations

SMTP and a direct Outlook writer remain external integrations. Hosted routing, mail transport and production migration belong to each installation.
The current optional calendar workflow uses the MCP client's external Outlook
app and requires no Microsoft credentials in this package.

# Design decisions

Earnings Watch describes the task and retains the `earnings_` MCP namespace. It combines an evidence-backed issuer calendar with document discovery; it is not a price feed or a forecast of earnings dates.

The design uses three familiar patterns: a pure validation/reconciliation core, a shared catalogue with personal projections, and a durable outbox. SQLite keeps backup and installation simple for one host. A database server, built-in OAuth issuer and plugin loader would add operational boundaries without improving this workload.

Model access uses user-owned agent CLIs. A raw API integration would introduce a second authentication and billing path. Extraction runs with tools disabled. Research has only search and scoped page/observation tools; page evidence must still pass deterministic validation. Browser automation is an extension boundary rather than a requirement for reading static issuer pages.

Maintenance is asynchronous because requests and model subprocesses have independent deadlines. The serving service remains synchronous and bounded. This is a deliberate deviation from a synchronous-core preference: I/O orchestration lives outside the pure engine and read service.

Side effects have explicit boundaries. R0 queries produce facts or plans. R1 operations update only personal state. Outbound notification delivery requires a separate notifier invocation. Calendar plan/ack delegates account effects to the client's optional Outlook app. No calendar feature grants arbitrary web content authority to invite people.

A single authoritative database replaces hand-edited state files. YAML, JSON, CSV and ICS are projections. Legacy import/export supports migration rehearsal; an uncertain legacy delivery remains unknown until an operator reconciles it. A failed image rollout does not restore database state.

# Architecture

One SQLite database owns both shared public facts and person-scoped state. Shared tables hold sources, events, revisions, polling state and documents. Personal tables hold follows, calendar ledgers, delivery receipts and downloads. Personal access always requires an explicit person selected at the authentication boundary.

CLI, MCP and Python callers share `PersonService`. It receives a read-only library reader and a personal store. The serving surface has no network, model or provider dependency. FastMCP runs synchronous service calls outside its event loop. SQL connections use WAL, five-second busy timeouts and explicit transactions; personal acknowledgements use immediate write transactions.

Maintenance runs separately under one process lock. Its stages register pending issuers, capture pages, extract claims, verify evidence, reconcile facts, poll official releases and prepare personal notification outboxes. A failed or incomplete extraction cannot establish absence. Removal caps guard catalogue commits. Models and web pages cannot write directly to the database.

Only `store/` imports SQLite. `transport.py` owns HTTP, including DNS validation and IP pinning with original-host TLS SNI. `llm/` owns model processes. The pure engine depends on models and the standard library. An AST gate enforces capability boundaries.

Calendar plans read follows, events, revision sequence and ledger in one snapshot. A signed handle binds that snapshot, person, sink, operations and thirty-minute expiry. Acknowledgement validates the handle and revisions in the same transaction that records confirmed writes. Retries of an acknowledged handle return its original receipt.

Event revisions increase across removal and reintroduction, so change consumers
and ICS clients never see a sequence reset. Personal download receipts describe
historical effects and survive catalogue retirement. They reference a document's
stable identifier without requiring that document to remain in the current
catalogue; new receipts validate current catalogue membership before insertion.

The library deliberately shares public facts. A public query can reveal that a source was registered or refreshed; it never returns another person's follows, tokens or calendar ledger. Operators who need to hide interest in an issuer must use separate deployments.

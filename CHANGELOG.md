# Changelog

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

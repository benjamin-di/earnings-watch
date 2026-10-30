# Providers

`generic_ir` captures a configured official issuer page, extracts text and links, and verifies model-proposed milestones. Set stable source keys, ticker, optional MIC exchange and ISIN, fiscal-year end and period scheme. Two exchanges may reuse a ticker; a watch without an exchange then returns candidates.

`sec_edgar` polls submissions for 8-K Item 2.02 and relevant 6-K filings, checks exhibits and guards against conflicting periods. Configure `[providers.sec_edgar] contact="operator@example.invalid"` with a real monitored contact address before enabling it. SEC requests refuse to start without a contact user agent. No API key is required.

`hkexnews` resolves the stock identifier and polls official result-announcement categories. It excludes conflicting reporting periods and stores seen announcement identifiers. Numeric tickers select this provider unless a SEC CIK is configured.

All provider requests pass through one transport: exact-host allowlist, public-address DNS checks, IP pinning, per-hop redirect checks, response limits, pacing, request budgets and a persisted 403/429 circuit breaker. Small validator-bearing responses use conditional GET. Large documents are downloaded explicitly and hashed.

Release polling starts at T−1 and ends at T+7, with at most 48 attempts per event. It polls every ten minutes through T+2, then thirty minutes, two hours and daily. Attempts are reserved before I/O so restarts cannot bypass the cap. Official release detection works with the model backend disabled.

Document verification requires official-host provenance, consistent period, role evidence and a verbatim quote. A link's presence is evidence of a document association, not an audit of its financial contents. Downloads are opt-in; unverified documents require an additional explicit opt-in. Dynamic pages and inaccessible documents may require operator assistance.

`EARNINGS_WATCH_PROVIDER_ALLOWLIST` accepts a comma-separated subset of
`generic_ir,sec_edgar,hkexnews`; an empty value disables acquisition. Unknown
provider names and inconsistent packaged definitions fail closed. Definition
budgets and pacing apply to the official provider hosts. Conditional page bodies
are limited to 64 small entries and the configured `cache_max_mb` byte budget.

Python integrations can inject `McpBrowserReader(client_factory, hosts)` into
`Maintenance(..., browser=reader)` for an HTTP failure. The configured MCP client
owns authentication (none, bearer, or an external private-key JWT provider).
The reader uses only start, navigate, deterministic extract and end, caps reads,
and validates returned URLs and text size. The trusted browser server must also
enforce public-address DNS and redirect policy; local checks cannot pin a remote
browser connection. Download links still pass through the HTTP transport.

A failed calendar read may trigger bounded model research for replacement official
URLs. Code probes the candidate, checks the issuer, and preserves the source key,
exchange, fiscal configuration and existing events. Failed sources are marked
in repair state and never deleted. Registration and repair retry at most daily
across worker restarts; all model work shares the per-run call budget.

Sources with `fetch="websearch"` and explicit `sweep --search KEY` use scoped
calendar research. An incomplete plain-page extraction can also search within
its remaining model budget. Every accepted claim must quote a saved official
capture; a search snippet or an uncaptured proposed URL cannot establish a date.
Research is never authoritative evidence that an older event disappeared.

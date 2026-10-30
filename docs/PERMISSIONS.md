# Permissions and trust

Local stdio and CLI are trusted, single-operator interfaces. The operator selects a person with `--person`; do not expose these commands as an unauthenticated remote shell. Database files, backups and `calendar.key` must remain private. The key signs calendar receipts and must be backed up with state.

Standalone HTTP authenticates a bearer token against the person encoded in `/users/<id>/mcp`. Tokens have at least 32 characters and are stored as SHA256 hashes. Rotation invalidates the previous token. An identity header has no authority in this mode.

Gateway HTTP accepts `/mcp` only with a configured backend bearer and `X-Fleet-Person` naming an existing person. Put this mode behind an admission gateway that validates the human identity and sets the header. Never expose the backend bearer to clients or trust a caller-supplied person header at public ingress. An operator who holds the backend bearer can select any person.

Allowed HTTP Hosts are explicit. Browser Origins must match the request authority. Requests over 256 KiB and tool responses over 128 KiB are refused. No cookies, OAuth issuer or Microsoft account authorization is built in. Use TLS at the reverse proxy for remote connections.

Read tools are R0. Following/unfollowing and acknowledging calendar writes are R1, idempotent personal mutations. Calendar acknowledgement is exempt from a second confirmation because it records operations already confirmed by the external app; it does not perform calendar writes. Publication and live account effects remain the operator's responsibility.

Issuer pages and model output are untrusted. The network boundary permits HTTPS on port 443, checks every resolved address and redirect, and pins the connection IP. Registration and research are budgeted. Model runners remove ambient MCP/project tools and inherited unrelated environment variables. A shared catalogue exposes public issuer interest indirectly; it is not an interest-confidential service.

# Permissions and trust

The CLI and stdio are trusted local operator interfaces. `--person` selects a
record only here. Keep the database, backups, browser sessions and `calendar.key`
private; restore the signing key with state. Restore changes the database ID,
invalidating old pagination and change cursors.

Company facts, events, evidence and documents form one shared catalogue. Follows,
holdings, mutes, preferences, feed tokens, downloads, calendar ledgers and delivery
receipts belong to one person. Shared facts expose no follower lists. Acquisition
covers the union of followed companies within the shared maintenance budget.

Standalone HTTP hashes a personal token and accepts it only at
`/users/<id>/mcp`. An identity header has no authority there. Use `users rotate`
to revoke a token and `users remove` to forget a record and its private state.

Hosted gateway HTTP requires the backend bearer plus `X-Fleet-Subject` from the
admission gateway. With an identity directory configured, it checks admission
before provisioning the immutable subject. `X-Fleet-Person` is only a display
label. Set `directory_required=true` to refuse the legacy, pre-created-person
fallback. Never give the backend key to a browser or public MCP client.

The optional Connections page uses public PKCE OIDC via signin-window. Configure
an explicit issuer, client, origin and allowed group. An optional independent
directory key resolves only an exact subject and current admission/email; it is
never a provider administrator key. Hosted deployment requires this directory.
Without it, the page uses the latest verified sign-in email. Disabling or removing
a standalone account is an operator responsibility.

Each browser form checks the exact Origin and current record revision. Holdings
imports require a signed, person-bound preview and atomic revision check. They
store company identities only, discarding quantities and financial values. Mutes
survive holdings refreshes. Registration has daily and five-pending person caps.

Calendar subscriptions use random bearer URLs stored by hash. Resetting a link
revokes its predecessor. With the directory enabled, group removal or disabled
accounts refuse feeds; a directory outage returns 503. Directory admission is
cached for at most one minute and mail preflight always uses a fresh lookup.
Disable access logging for feed paths at the server and proxy. Keep feeds private.

Calendar plan/ack records only selected events or previously delegated entries.
It performs no Outlook write and owns no calendar credential. Unfollowing never
deletes a personal calendar entry. Calendar feeds remove unfollowed events on the
calendar application's next refresh; refresh timing belongs to that application.

New people receive results-out notifications; daily briefs and urgent changes are
off until enabled. Queued notifications honor current preferences and follows.
Submission starts with a durable unknown state; timeouts never cause automatic
replay. `notify --inspect` exposes pending/unknown submissions to the operator.
`notify --resolve KEY --receipt FILE` requires proof bound to the person, key, current attempt and payload hash,
with outcome `submitted` (and message receipt) or `not_submitted`. A submission
receipt establishes transport acceptance, not arrival in a recipient's inbox.

Issuer pages and model output are untrusted. HTTP acquisition pins validated public
HTTPS addresses and checks redirects. Model runners remove unrelated environment,
project tools and ambient MCP. Serving never acquires issuer data or invokes a
model. Host allowlists, body and response budgets apply to remote reads. Use TLS
and a separate credential boundary for every hosted service role.

# Connections setup and use

Sign in to see your next 30 days, add a company from the shared catalogue, or
request an unlisted ticker. Import holdings by pasting tickers/ISINs or uploading
a CSV; review the preview before replacement. Only company identities are kept.
Uploaded holdings, operator-synced holdings and the manual watchlist are separate.
Mute a holding to hide it across reads, notifications and feeds; the mute survives
refreshes. Exchange-qualified identities avoid ambiguous ticker selection.

Notifications use your current sign-in directory email, shown read-only. New
people start with results-out enabled, daily brief and urgent changes disabled.
Choose your timezone and brief hour. Quiet days produce no brief. A submission
receipt confirms transport acceptance, not delivery to your inbox.

Subscribe from Calendar, or copy its private link into your calendar app's
subscribe-from-web setting. The link grants read access to your earnings events;
keep it private. Resetting it stops the old subscription. Calendar apps choose
refresh timing, often hours. For a particular event in your main calendar, ask
your MCP client to plan that event and apply it through your Outlook connector.
Earnings stores no calendar credentials. Unfollowing never deletes a delegated
personal-calendar entry.

## Operator setup

Initialize private persistent state with `earnings-watch init` before serving.
For browser sign-in, configure the public PKCE client at your identity provider:
callback `https://earnings.example/connections/callback`, launch
`https://earnings.example/connections/`. Example configuration:

```toml
[connections]
public_url = "https://earnings.example"
oidc_issuer = "https://identity.example"
oidc_client_id = "earnings-connections"
oidc_required_group = "earnings-users"

[serve]
allowed_hosts = ["earnings.example", "localhost"]
```

Serve with `earnings-watch serve --http --host 0.0.0.0 --mode standalone` behind
TLS. Standalone MCP users still need individual tokens from `users add`; browser
accounts are keyed by the verified immutable OIDC subject. The browser uses only
verified sign-in email without a directory. `users remove SUBJECT` deletes and
blocks recreation; explicit `users add SUBJECT` re-enrolls it.

Hosted mode additionally requires an exact-subject identity directory,
`directory_required = true`, and separate backend bearer environment references.
The admission gateway must overwrite `X-Fleet-Subject` from verified identity;
`X-Fleet-Person` is a display label. The directory returns only current admission,
email and display identity. Missing directory service refuses access rather than
using stale admission. `/monitor/mcp` exposes only shared acquisition status to
the backend credential, without creating a personal account.

Protect the database, signing key and browser state. Disable server/proxy access
logs, especially private calendar paths. Use role-separated server, worker and
notification processes. Back up and rehearse restoration before upgrading state;
`init` explicitly upgrades schema 1/2 to 3. Stop all writers for migration and
preserve original external-effect evidence. See [permissions](PERMISSIONS.md)
and [operations](OPERATIONS.md) for receipts and recovery.

# Security

Use GitHub's private vulnerability reporting for security issues. Never publish credentials, database files, calendar handles or raw private diagnostics in an issue.

Keep the data/auth volumes private and use TLS for remote HTTP. Standalone per-person tokens and gateway backend credentials have different authority; see [permissions](docs/PERMISSIONS.md). Do not expose gateway mode directly to untrusted callers.

Security-relevant fixes receive a new release. Treat model output, captured pages, document links and mirror contributions as untrusted inputs. Financial dates and document verification are evidence checks, not investment advice or financial-content certification.

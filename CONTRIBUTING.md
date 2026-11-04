# Contributing

Report bugs with synthetic inputs and redacted diagnostics. Do not attach account tokens, personal follows, calendar records or captured private pages.

## How changes land

This repository is the maintained source for Earnings Watch. Open issues and pull requests here. Maintainers accept generic fixes, synthetic tests and public documentation directly after review and the required checks pass. Contributions remain in public history across releases. Include a focused regression test for a bug fix and describe the behavior and validation in the pull request.

Keep deployment runbooks, credentials, personal records and operational evidence outside this repository. Do not change the source-home contract or publication permissions as part of an unrelated fix. See [Source and releases](docs/SOURCE.md).

Run `make bootstrap` and `make check`. Keep network, models and account effects behind fake boundaries in offline tests. Add a regression test when changing date, period, timing, persistence or authentication behavior. Public fixtures must be fictional.

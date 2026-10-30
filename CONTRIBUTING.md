# Contributing

Report bugs with synthetic inputs and redacted diagnostics. Do not attach account tokens, personal follows, calendar records or captured private pages.

## How changes land

This repository is a release mirror. Maintained source is reviewed separately and exported as complete releases. Pull requests are reviewed and reimplemented in that source, then credited in release notes; they are not merged directly into the mirror because the next export would replace them.

Run `make bootstrap` and `make check`. Keep network, models and account effects behind fake boundaries in offline tests. Add a regression test when changing date, period, timing, persistence or authentication behavior. Public fixtures must be fictional.

.PHONY: bootstrap check test live conformance
bootstrap:
	uv sync --locked
check:
	uv lock --check
	uv run --locked ruff check src tests scripts
	uv run --locked ruff format --check src tests scripts
	uv run --locked mypy src
	uv run --locked python scripts/check-boundaries.py
	uv run --locked pytest -m 'not live' -q
	uv build --no-sources
	git diff --check

test:
	uv run --locked pytest -q
live:
	uv run --locked pytest -m live private/ -q

conformance:
	uv run --locked python scripts/check-conformance.py

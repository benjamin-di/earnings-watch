.PHONY: bootstrap check test live conformance ci-check ci-package ci-images
JUNIT_XML ?=
bootstrap:
	uv sync --locked
check:
	@if test -f .public-source.toml; then uv run --locked python scripts/check-public-source.py; fi
	uv lock --check
	uv run --locked ruff check src tests scripts
	uv run --locked ruff format --check src tests scripts
	uv run --locked mypy src
	uv run --locked python scripts/check-boundaries.py
	uv run --locked pytest -m 'not live' -q $(if $(JUNIT_XML),--junitxml=$(JUNIT_XML))
	uv build --no-sources
	git diff --check

test:
	uv run --locked pytest -q
live:
	uv run --locked pytest -m live private/ -q

conformance:
	uv run --locked python scripts/check-conformance.py

# Native source checks retain the complete project gate and require a real report.
ci-check: bootstrap
	mkdir -p reports/junit
	$(MAKE) check JUNIT_XML=reports/junit/check.xml
	test -s reports/junit/check.xml

# Tag and manual source-package validation; deployment images have their own publisher.
ci-package:
	python3 scripts/ci-package.py

ci-images:
	python3 scripts/ci-images.py --arch amd64
	python3 scripts/ci-images.py --arch arm64

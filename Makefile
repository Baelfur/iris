.PHONY: setup test lint

setup:
	pip install -e './core[test,lint,metrics,tracing,config-git,config-db,kafka]'

test:
	cd core && pytest tests/

# Mirrors the .github/workflows/lint.yml steps exactly so `make lint`
# passes locally iff CI lint passes. Don't drift this without also
# updating the workflow (or vice versa) — local/CI disagreement is
# the bug the external review flagged's wider audit.
lint:
	ruff check core/core variants
	ruff format --check core/core variants
	cd core && mypy core
	python docs/build.py --check
	interrogate -c core/pyproject.toml core/core

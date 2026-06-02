# praiselul — common dev & release tasks
#
# Uses the project virtualenv at ./venv by default. Override with:
#   make PYTHON=python3 <target>

PYTHON ?= venv/bin/python

.DEFAULT_GOAL := help

.PHONY: help install test lint clean build check prepare publish

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## Create venv and install the package with dev + release tooling
	python3 -m venv venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]" build twine

test: ## Run the test suite
	$(PYTHON) -m pytest tests

lint: ## Lint with ruff
	$(PYTHON) -m ruff check .

clean: ## Remove build artifacts (dist/, build/, *.egg-info)
	rm -rf dist build *.egg-info

build: clean ## Clean, then build wheel + sdist into dist/
	$(PYTHON) -m build

check: build ## Validate the built artifacts with twine
	$(PYTHON) -m twine check dist/*

prepare: test lint check ## Full pre-publication check: test, lint, build, validate
	@echo "Artifacts ready in dist/. Run 'make publish' to upload to PyPI."

publish: prepare ## Upload dist/* to PyPI (requires credentials)
	$(PYTHON) -m twine upload dist/*

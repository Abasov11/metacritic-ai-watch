VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: help venv dev test lint format audit crawl-once refresh-covers docker-up docker-down

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t22

$(VENV):
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -e ".[dev]"

venv: $(VENV)  ## create .venv and install dependencies

dev: $(VENV)  ## run the app locally on http://127.0.0.1:8010
	$(VENV)/bin/uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload

test: $(VENV)  ## run the test suite (never touches the network)
	$(VENV)/bin/pytest -q

lint: $(VENV)  ## ruff check + format check
	$(VENV)/bin/ruff check app tests
	$(VENV)/bin/ruff format --check app tests

audit: $(VENV)  ## check dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit

format: $(VENV)  ## apply ruff formatting
	$(VENV)/bin/ruff format app tests
	$(VENV)/bin/ruff check --fix app tests

crawl-once: $(VENV)  ## run a single crawl now (LIMIT=3 to cap it)
	$(PY) -m app.crawler --once $(if $(LIMIT),--limit $(LIMIT),)

refresh-covers: $(VENV)  ## re-download covers for every game in the database
	$(PY) -m app.crawler --refresh-covers

docker-up:  ## build and start the container in the background
	docker compose up -d --build

docker-down:  ## stop the container
	docker compose down

.PHONY: install test lint type up down ping
install:
	pip install -e ".[dev]"
test:
	pytest -q
lint:
	ruff check app tests
type:
	mypy app
up:
	docker compose -f docker/docker-compose.dev.yml up --build
down:
	docker compose -f docker/docker-compose.dev.yml down
ping:
	python -m app.tools.ping

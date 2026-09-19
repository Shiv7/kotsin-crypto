.PHONY: setup test test-live lint run ui-dev ui-build

setup:
	cd backend && uv sync
	cd frontend && npm install

test:
	cd backend && uv run pytest -m "not live" -q

test-live:
	cd backend && uv run pytest -m live -q

lint:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run lint-imports

run:
	cd backend && uv run kotsin-crypto

ui-dev:
	cd frontend && npm run dev

ui-build:
	cd frontend && npm run build

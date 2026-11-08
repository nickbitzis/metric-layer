PORT ?= 8840
ORDERS ?= 20000
ML = uv run metriclayer

.PHONY: install test lint format build verify eval eval-claude report run docker-build screenshot clean

install:
	uv sync --python 3.12

test:
	uv run pytest -q

lint:
	uvx ruff check .

format:
	uvx ruff format .
	uvx ruff check . --fix

build:  ## generate the synthetic warehouse into data/warehouse.duckdb
	$(ML) build --orders $(ORDERS)

verify:  ## compiled vs reference SQL; exits 1 on any mismatch
	$(ML) verify

eval:  ## natural language eval with the offline matcher
	$(ML) nl-eval --show-misses

eval-claude:  ## same eval with Claude (needs ANTHROPIC_API_KEY and uv sync --extra claude)
	$(ML) nl-eval --provider claude --show-misses

report:  ## write docs/results.md and docs/results.json
	$(ML) report

run:
	PORT=$(PORT) $(ML) serve

docker-build:
	docker build -t metric-layer .

screenshot:  ## needs the server running on $(PORT) and Google Chrome
	uv run python scripts/screenshot.py --port $(PORT)

clean:
	rm -rf .pytest_cache .ruff_cache data

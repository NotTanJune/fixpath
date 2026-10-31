.PHONY: offline live setup test ontology data testset ingest map api api-replay warm brand brand-check golden evals web web-build deck deck-private

setup:
	uv sync

test:
	uv run pytest -q

ontology:
	uv run python -m app.ontology export

data:
	uv run python -m app.generator

testset:
	uv run python -m app.generator.testset

ingest:
	uv run python -m app.ingest

map:
	uv run --group map python -m app.factmap

api:
	uv run python -m app.api

api-replay:
	uv run python -m app.api --replay

warm:
	uv run python -m app.api warm

golden:
	uv run python -m app.evals.golden

evals:
	uv run python -m app.evals --layers 1,2,3,4,5 --bakeoff

brand:
	uv run python scripts/brand.py render

brand-check:
	uv run python scripts/brand.py check

web:
	cd web && bun run dev

# Both run the API and the web UI together in one terminal. Ctrl-C stops both.
# offline: replay mode, no keys, no spend. Uploads are refused; "Watch a recorded ingest" works.
offline:
	cd web && bunx concurrently --kill-others --names api,web --prefix-colors blue,green \
		"cd .. && uv run python -m app.api --replay" "bun run dev"

# live: real keys from .env, real backend. Ingests new files and answers any question (paid calls).
live:
	@uv run python -c "from app.config import load_settings; s = load_settings(); import sys; sys.exit(0 if s.has_openai and s.has_typesafe else 'make live needs OPENAI_API_KEY and TYPESAFE_API_KEY in .env or the environment.')"
	cd web && bunx concurrently --kill-others --names api,web --prefix-colors blue,green \
		"cd .. && uv run python -m app.api" "bun run dev"

web-build:
	cd web && bun run build

deck:
	uv run python scripts/brand.py render
	uv run python scripts/deck.py diagrams
	uv run python scripts/deck.py pdf

deck-private:
	uv run python scripts/brand.py render
	uv run python scripts/deck.py diagrams
	uv run python scripts/deck.py private

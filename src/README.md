# src

The Python package.

- `ingest/` is the ingestion crew: it reads files, extracts facts, merges and links them, checks them and loads the graph.
- `ask/` is the troubleshooting crew: triage, two lookup agents, a writer, a checker and the answer card.
- `decide/` holds the two backends for the fast decisions, JEV and OpenAI Decisions.
- `api/` is the FastAPI server and its event streams.
- `evals/` measures the system against the hidden truth.
- `generator/` writes the simulated plant files.
- `ontology.py` defines the node types and relations once.
- `store.py` reads and writes the SQLite graph, text chunks and search.
- `llm.py`, `llm_cache.py` and `ledger.py` call the models, cache their answers and track spend.
- `embed.py` turns text into vectors, and `factmap.py` places them on the 3D map.
- `config.py` holds paths and settings, and `brand.py` reads the product name.

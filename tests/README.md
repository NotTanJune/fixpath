# tests

The Python tests, grouped by the part of the code they cover. `make test` runs them all.

- `api/`, `ask/`, `decide/`, `evals/`, `generator/`, `ingest/` follow the folders of `src/app/`.
- `core/` covers the shared modules: config, storage, models, ledger, embeddings and the ontology.
- `scripts/` covers the helpers in `scripts/`.
- `stubs/` holds the fake models and fake data that many tests share.
- `live/` holds tests that call real models. They run only when API keys are set.

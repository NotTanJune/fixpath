# data

Everything the system reads or writes while it runs.

- `raw/` holds the simulated plant files: machine logs, handover notes, PDF manuals, maintenance exports and the spares workbook. The ingestion crew reads them.
- `truth/` holds the hidden true plant that generated those files. Only the evals and the tests read it. The pipeline never does.
- `kg.sqlite` is the knowledge graph, with its text chunks and full-text index.
- `map/` holds the points of the 3D fact map.
- `pipeline/` holds the report of the last ingestion run.
- `cache/` holds the saved model and decision answers that let the app run without an API key.
- `evals/` holds the golden question sets and the eval results.
- `recordings/` holds saved API answers (`api/`) and saved ingestion runs (`ingest/`) for the web app's tests and for replay.
- `testset/` holds ten extra files to ingest and five to validate, with the facts each should give.

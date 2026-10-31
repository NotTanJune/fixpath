# scripts

Command-line helpers that sit outside the app itself.

- `api_fixtures.py` saves the API's answers into `data/recordings/api/`.
- `brand.py` fills the deck template from `config/brand.json` and checks that the product name appears only there.
- `deck.py` renders the diagrams and builds the deck PDF.
- `testset_run.py` runs the files in `data/testset/` through ingestion and reports which expected facts were found.

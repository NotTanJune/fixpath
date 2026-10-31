# config

- `brand.json` holds the product name and its short texts. The deck, the web page head and the name check read it.
- `ontology.json` lists the node types and relations of the knowledge graph. `make ontology` writes it from `src/app/ontology.py`, and the API and the web app read it.

"""Ingestion: turns the raw files into facts for the knowledge graph.

Each extractor reads one kind of file and proposes facts. Later stages match those
facts to things the graph already knows, check them and store them.
"""

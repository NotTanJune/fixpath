"""Turn text into lists of numbers that capture meaning, using a local model.

`Embedder` loads the model on first use and downloads its files into a cache folder
that git ignores. `embed_passages` handles chunks of text and `embed_query` handles a
search question. Both use one shared model.
"""

from collections.abc import Iterable
from pathlib import Path

import numpy as np

from app.config import ROOT

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384
MODEL_DIR = ROOT / ".cache" / "fastembed"


def _unit_length(vectors: np.ndarray) -> np.ndarray:
    """Scale each vector to length 1. An all-zero vector stays zero."""
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)


class Embedder:
    def __init__(self, model_name: str = MODEL_NAME, cache_dir: Path = MODEL_DIR):
        self.model_name = model_name
        self.cache_dir = Path(cache_dir)
        self._model = None

    @property
    def model(self):
        """The embedding model, loaded on first use."""
        if self._model is None:
            from fastembed import TextEmbedding  # slow to import, so load it late

            self._model = TextEmbedding(
                model_name=self.model_name, cache_dir=str(self.cache_dir)
            )
        return self._model

    def embed_passages(self, texts: Iterable[str]) -> np.ndarray:
        """Turn text chunks into number lists, one for each chunk."""
        texts = list(texts)
        if not texts:
            return np.empty((0, DIMENSIONS), dtype=np.float32)
        vectors = np.vstack(list(self.model.passage_embed(texts)))
        return _unit_length(vectors.astype(np.float32, copy=False))

    def embed_query(self, text: str) -> np.ndarray:
        """Turn a search query into one list of numbers."""
        vector = np.asarray(next(iter(self.model.query_embed(text))))
        return _unit_length(vector.astype(np.float32, copy=False))


_default = Embedder()


def embed_passages(texts: Iterable[str]) -> np.ndarray:
    """Embed text chunks with the shared model."""
    return _default.embed_passages(texts)


def embed_query(text: str) -> np.ndarray:
    """Embed a search query with the shared model."""
    return _default.embed_query(text)

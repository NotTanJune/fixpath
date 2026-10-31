"""Tests for the text embedder, using a fake model. The real model is downloaded on first
use and is covered by the live tests."""

from pathlib import Path

import numpy as np
import pytest

from app import embed
from app.config import ROOT

DIMENSIONS = 384


class FakeTextEmbedding:
    """Stands in for the real embedding model. Its vectors are not scaled to length 1,
    so a test can tell whether the caller scaled them."""

    instances: list["FakeTextEmbedding"] = []

    def __init__(self, model_name: str, cache_dir: str | None = None, **kwargs):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []
        FakeTextEmbedding.instances.append(self)

    @staticmethod
    def vector(text: str) -> np.ndarray:
        rng = np.random.default_rng(sum(map(ord, text)))
        return (rng.normal(size=DIMENSIONS) * 3).astype(np.float32)

    def passage_embed(self, texts, **kwargs):
        texts = list(texts)
        self.passage_calls.append(texts)
        for text in texts:
            yield self.vector(text)

    def query_embed(self, query, **kwargs):
        self.query_calls.append(query)
        yield self.vector(query)


@pytest.fixture(autouse=True)
def fake_fastembed(monkeypatch):
    FakeTextEmbedding.instances = []
    monkeypatch.setattr("fastembed.TextEmbedding", FakeTextEmbedding)
    return FakeTextEmbedding


def test_the_model_is_bge_small_and_its_files_live_in_the_ignored_cache_folder():
    assert embed.MODEL_NAME == "BAAI/bge-small-en-v1.5"
    assert embed.DIMENSIONS == DIMENSIONS
    assert embed.MODEL_DIR == ROOT / ".cache" / "fastembed"
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".cache/" in ignored  # the model files must never be committed


def test_the_model_loads_lazily_and_only_once(fake_fastembed):
    embedder = embed.Embedder()
    assert fake_fastembed.instances == []  # building an Embedder loads nothing
    embedder.embed_query("first")
    embedder.embed_passages(["a", "b"])
    embedder.embed_query("second")
    assert len(fake_fastembed.instances) == 1


def test_the_model_is_loaded_with_the_name_and_cache_folder(fake_fastembed, tmp_path):
    embed.Embedder().embed_query("x")
    (model,) = fake_fastembed.instances
    assert model.model_name == "BAAI/bge-small-en-v1.5"
    assert Path(model.cache_dir) == ROOT / ".cache" / "fastembed"

    embed.Embedder("some/model", tmp_path).embed_query("x")
    other = fake_fastembed.instances[-1]
    assert other.model_name == "some/model"
    assert Path(other.cache_dir) == tmp_path


def test_embed_passages_returns_unit_length_float32_rows_in_input_order(fake_fastembed):
    texts = ["seal kit", "spindle chiller", "gripper finger"]
    vectors = embed.Embedder().embed_passages(texts)

    assert vectors.shape == (3, DIMENSIONS)
    assert vectors.dtype == np.float32
    assert np.linalg.norm(vectors, axis=1) == pytest.approx(1.0, abs=1e-6)
    for row, text in zip(vectors, texts):
        expected = FakeTextEmbedding.vector(text)
        assert row == pytest.approx(expected / np.linalg.norm(expected), abs=1e-6)
    (model,) = fake_fastembed.instances
    assert model.passage_calls == [texts]  # chunks go through passage_embed
    assert model.query_calls == []


def test_embed_passages_accepts_any_iterable_of_texts():
    vectors = embed.Embedder().embed_passages(text for text in ["a", "b"])
    assert vectors.shape == (2, DIMENSIONS)


def test_embed_passages_of_nothing_is_an_empty_matrix_and_loads_no_model(
    fake_fastembed,
):
    vectors = embed.Embedder().embed_passages([])
    assert vectors.shape == (0, DIMENSIONS)
    assert vectors.dtype == np.float32
    assert fake_fastembed.instances == []


def test_embed_query_returns_one_unit_length_float32_vector(fake_fastembed):
    vector = embed.Embedder().embed_query("why does the chiller alarm")
    assert vector.shape == (DIMENSIONS,)
    assert vector.dtype == np.float32
    assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-6)
    (model,) = fake_fastembed.instances
    assert model.query_calls == ["why does the chiller alarm"]  # query_embed
    assert model.passage_calls == []


def test_a_zero_vector_stays_zero_instead_of_turning_into_nan(monkeypatch):
    monkeypatch.setattr(
        FakeTextEmbedding, "vector", staticmethod(lambda text: np.zeros(DIMENSIONS))
    )
    embedder = embed.Embedder()
    assert not np.isnan(embedder.embed_passages(["x"])).any()
    assert not np.isnan(embedder.embed_query("x")).any()
    assert embedder.embed_query("x").shape == (DIMENSIONS,)


def test_the_module_functions_share_one_default_embedder(monkeypatch, fake_fastembed):
    monkeypatch.setattr(embed, "_default", embed.Embedder())
    assert embed.embed_passages(["a", "b"]).shape == (2, DIMENSIONS)
    assert embed.embed_query("a").shape == (DIMENSIONS,)
    assert len(fake_fastembed.instances) == 1

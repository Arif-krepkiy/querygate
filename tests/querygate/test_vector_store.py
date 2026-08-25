"""Vector backend contract and hybrid search, driven by a fake embedder."""

from __future__ import annotations

import os

import pytest

np = pytest.importorskip("numpy", reason="install querygate[embeddings] to run these")

from querygate import config  # noqa: E402
from querygate.catalog.loaders import bundle  # noqa: E402
from querygate.retrieval.index import CatalogIndex  # noqa: E402
from querygate.retrieval.vector_store import InProcessVectors, create_vector_backend  # noqa: E402


class FakeEmbedder:
    """Deterministic 8-dim vectors: a bag-of-words hash, no model required."""

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = []
        for text in texts:
            vec = np.zeros(8, dtype="float32")
            for token in text.lower().split():
                vec[hash(token) % 8] += 1.0
            vectors.append(vec)
        return np.array(vectors, dtype="float32")


@pytest.fixture
def catalog():
    return bundle.load_bundle(config.CATALOG_LOCAL_PATH)


class TestInProcessBackend:
    def test_upsert_then_search_ranks_nearest_first(self):
        backend = InProcessVectors()
        vectors = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype="float32")
        backend.upsert(["a", "b", "c"], vectors)
        hits = backend.search(np.array([1.0, 0.0], dtype="float32"), limit=3)
        assert [name for name, _ in hits][:2] == ["a", "b"]

    def test_empty_backend_returns_nothing(self):
        assert InProcessVectors().search(np.array([1.0, 0.0], dtype="float32"), limit=5) == []

    def test_normalisation_makes_magnitude_irrelevant(self):
        """Cosine similarity: a long vector must not outrank a better-aligned one."""
        backend = InProcessVectors()
        backend.upsert(["aligned", "long"], np.array([[1.0, 0.0], [5.0, 5.0]], dtype="float32"))
        hits = backend.search(np.array([1.0, 0.0], dtype="float32"), limit=2)
        assert hits[0][0] == "aligned"

    def test_upsert_replaces_previous_set(self):
        backend = InProcessVectors()
        backend.upsert(["old"], np.array([[1.0, 0.0]], dtype="float32"))
        backend.upsert(["new"], np.array([[1.0, 0.0]], dtype="float32"))
        assert [n for n, _ in backend.search(np.array([1.0, 0.0], dtype="float32"), 5)] == ["new"]


class TestHybridSearch:
    def test_index_uses_the_backend(self, catalog):
        index = CatalogIndex.build(catalog, embedder=FakeEmbedder())
        assert index._vectors is not None
        assert index.search("revenue", 3)

    def test_injected_backend_is_used(self, catalog):
        """The seam: pass any VectorBackend and the index never knows the difference."""
        backend = InProcessVectors()
        index = CatalogIndex.build(catalog, embedder=FakeEmbedder(), vectors=backend)
        assert index._vectors is backend
        assert index.search("plans and pricing", 3)

    def test_broken_embedder_degrades_to_bm25(self, catalog):
        class Broken:
            def embed(self, texts):
                raise RuntimeError("model failed to load")

        index = CatalogIndex.build(catalog, embedder=Broken())
        assert index._vectors is None
        # Search still works: embeddings are additive, never required.
        assert "plan_catalog" in [m.name for m in index.search("plan catalog", 3)]

    def test_backend_failure_at_query_time_degrades(self, catalog):
        class BrokenBackend(InProcessVectors):
            def search(self, query_vector, limit):
                raise RuntimeError("vector store unreachable")

        index = CatalogIndex.build(catalog, embedder=FakeEmbedder(), vectors=BrokenBackend())
        # BM25 still ranks, so the caller gets results rather than an error.
        assert index.search("plan catalog", 3)


class TestFactory:
    def test_defaults_to_in_process(self, monkeypatch):
        monkeypatch.setattr(config, "VECTOR_BACKEND", "memory")
        assert isinstance(create_vector_backend(dim=8), InProcessVectors)

    def test_qdrant_unreachable_falls_back(self, monkeypatch):
        """An unreachable vector store must degrade, not break startup."""
        monkeypatch.setattr(config, "VECTOR_BACKEND", "qdrant")
        monkeypatch.setattr(config, "QDRANT_URL", "http://127.0.0.1:1")  # nothing listening
        assert isinstance(create_vector_backend(dim=8), InProcessVectors)


_QDRANT_URL = os.environ.get("QG_TEST_QDRANT_URL")


@pytest.mark.skipif(not _QDRANT_URL, reason="set QG_TEST_QDRANT_URL to test the Qdrant backend")
class TestQdrantBackend:
    """Same contract as the in-process backend, against a real server."""

    @pytest.fixture
    def backend(self):
        import uuid

        from querygate.retrieval.vector_store import QdrantVectors

        store = QdrantVectors(_QDRANT_URL, f"qgtest_{uuid.uuid4().hex[:8]}", dim=2)
        yield store
        store.close()

    def test_upsert_then_search(self, backend):
        backend.upsert(["a", "b", "c"], np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype="float32"))
        hits = backend.search(np.array([1.0, 0.0], dtype="float32"), limit=3)
        assert [name for name, _ in hits][:2] == ["a", "b"]

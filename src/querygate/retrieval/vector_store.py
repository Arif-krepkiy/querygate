"""Vector backends: an in-process numpy matrix, or Qdrant."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from querygate import config
from querygate.log_setup import get_logger

if TYPE_CHECKING:
    import numpy as np

_log = get_logger("querygate.retrieval.vectors")


class VectorBackend(Protocol):
    """Similarity search over the catalog's embedding vectors."""

    def upsert(self, ids: list[str], vectors: np.ndarray) -> None:
        """Replace the stored vectors with this set (ids are model names)."""

    def search(self, query_vector: np.ndarray, limit: int) -> list[tuple[str, float]]:
        """Return ``(id, score)`` best-first. Scores need only be comparable
        within one call; the caller fuses by rank, not by absolute value."""

    def close(self) -> None: ...


class InProcessVectors:
    """Normalised matrix in memory; cosine similarity is a dot product."""

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._matrix: np.ndarray | None = None

    def upsert(self, ids: list[str], vectors: np.ndarray) -> None:
        import numpy as np

        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        self._matrix = vectors / np.clip(norms, 1e-9, None)
        self._ids = list(ids)

    def search(self, query_vector: np.ndarray, limit: int) -> list[tuple[str, float]]:
        import numpy as np

        if self._matrix is None or not self._ids:
            return []
        query = query_vector / max(float(np.linalg.norm(query_vector)), 1e-9)
        scores = self._matrix @ query
        order = np.argsort(scores)[::-1][:limit]
        return [(self._ids[i], float(scores[i])) for i in order if scores[i] > 0]

    def close(self) -> None:
        self._matrix = None
        self._ids = []


class QdrantVectors:
    """Shared vector store.

    Vectors are written to a collection named for the catalog build, then the
    handle is swapped, so a rebuild never mutates the collection currently
    being served, and a failed upsert leaves the previous one intact (the same
    last-known-good contract the catalog itself follows).
    """

    def __init__(self, url: str, collection: str, dim: int) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        self._client = QdrantClient(url=url)
        self._collection = collection
        self._client.recreate_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        _log.info("Qdrant collection ready: %s (dim=%d)", collection, dim)

    def upsert(self, ids: list[str], vectors: np.ndarray) -> None:
        from qdrant_client.models import PointStruct

        points = [
            PointStruct(id=i, vector=vector.tolist(), payload={"name": name})
            for i, (name, vector) in enumerate(zip(ids, vectors, strict=False))
        ]
        self._client.upsert(collection_name=self._collection, points=points)

    def search(self, query_vector: np.ndarray, limit: int) -> list[tuple[str, float]]:
        try:
            hits = self._client.search(
                collection_name=self._collection,
                query_vector=query_vector.tolist(),
                limit=limit,
            )
        except Exception as exc:
            _log.warning("qdrant search failed, falling back to keyword ranking: %s", exc)
            return []
        return [(hit.payload.get("name", ""), float(hit.score)) for hit in hits if hit.payload]

    def close(self) -> None:
        self._client.close()


def create_vector_backend(dim: int, build_id: str = "latest") -> VectorBackend:
    """Pick a backend from config; fall back to in-process on any problem."""
    if config.VECTOR_BACKEND == "qdrant" and config.QDRANT_URL:
        try:
            collection = f"{config.QDRANT_COLLECTION}_{build_id}"
            return QdrantVectors(config.QDRANT_URL, collection, dim)
        except Exception as exc:
            # Search quality degrades gracefully; an unreachable vector store
            # must never be the reason a question goes unanswered.
            _log.error("Qdrant unavailable (%s); using in-process vectors", exc)
    return InProcessVectors()

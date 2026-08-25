"""Catalog search index: BM25 always, embeddings optionally, fused with RRF."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rank_bm25 import BM25Okapi

from querygate.log_setup import get_logger
from querygate.retrieval.vector_store import VectorBackend, create_vector_backend

if TYPE_CHECKING:
    from querygate.catalog.models import CatalogModel, SemanticCatalog

_log = get_logger("querygate.retrieval")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, and break identifier boundaries
    so ``monthly_revenue`` and ``MonthlyRevenue`` both yield [monthly, revenue]."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return _TOKEN_RE.findall(spaced.lower())


def _model_document(model: CatalogModel) -> list[str]:
    parts = [model.name, model.domain, model.description]
    parts.extend(c.name for c in model.columns)
    parts.extend(c.description for c in model.columns)
    tokens: list[str] = []
    for part in parts:
        tokens.extend(tokenize(part))
    return tokens


def _embedding_text(model: CatalogModel) -> str:
    cols = ", ".join(c.name for c in model.columns[:20])
    return f"{model.name} in {model.domain}: {model.description}. Columns: {cols}"


def _expand(tokens: list[str], glossary: dict[str, tuple[str, ...]]) -> list[str]:
    out = list(tokens)
    for token in tokens:
        for syn in glossary.get(token, ()):  # term → synonyms
            out.extend(tokenize(syn))
    return out


def _rrf(ranked_lists: list[list[int]], limit: int, *, k: int = 60) -> list[int]:
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, idx in enumerate(ranked):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    return [idx for idx, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]]


@dataclass(frozen=True)
class CatalogIndex:
    catalog: SemanticCatalog
    _models: tuple[CatalogModel, ...]
    _bm25: BM25Okapi
    # Vector similarity lives behind a backend (in-process matrix by default,
    # Qdrant when configured) so this class never learns where vectors are
    # stored. See retrieval/vector_store.py.
    _vectors: VectorBackend | None = field(default=None)
    _embedder: object | None = field(default=None)

    @classmethod
    def build(
        cls,
        catalog: SemanticCatalog,
        embedder: object | None = None,
        vectors: VectorBackend | None = None,
    ) -> CatalogIndex:
        models = catalog.models
        # Enrich each document with glossary synonyms so a query term like
        # "money" reaches a model whose text only says "revenue". Query-side
        # expansion (in search) covers the reverse direction.
        docs = [_expand(_model_document(m), catalog.glossary) for m in models]
        bm25 = BM25Okapi(docs)

        embedder = embedder or _default_embedder()
        if embedder is None:
            return cls(catalog=catalog, _models=models, _bm25=bm25)

        try:
            matrix = embedder.embed([_embedding_text(m) for m in models])  # type: ignore[attr-defined]
            backend = vectors or create_vector_backend(
                dim=int(matrix.shape[1]),
                build_id=catalog.build.get("git_sha", "latest"),
            )
            backend.upsert([m.name for m in models], matrix)
            _log.info("embeddings enabled (%d models, %s)", len(models), type(backend).__name__)
            return cls(catalog=catalog, _models=models, _bm25=bm25, _vectors=backend, _embedder=embedder)
        except Exception as exc:
            _log.warning("embeddings unavailable, using BM25 only: %s", exc)
            return cls(catalog=catalog, _models=models, _bm25=bm25)

    def search(self, query: str, limit: int = 10) -> list[CatalogModel]:
        tokens = _expand(tokenize(query), self.catalog.glossary)
        if not tokens:
            return []
        bm25_rank = _positive_desc(self._bm25.get_scores(tokens))

        if self._vectors is None:
            chosen = bm25_rank[:limit]
        else:
            # Rank fusion happens here, in-process, whichever backend served
            # the vector side, which is what makes the backend swappable.
            chosen = _rrf([bm25_rank, self._vector_rank(query, limit)], limit)
        return [self._models[i] for i in chosen]

    def _vector_rank(self, query: str, limit: int) -> list[int]:
        """Model indices ordered by vector similarity, best first."""
        try:
            query_vector = self._embedder.embed([query])[0]  # type: ignore[attr-defined]
            # Over-fetch: fusion needs a deeper vector list than the final cut.
            hits = self._vectors.search(query_vector, max(limit * 3, 30))  # type: ignore[union-attr]
        except Exception as exc:
            _log.warning("vector ranking failed, BM25 only for this query: %s", exc)
            return []
        positions = {model.name: i for i, model in enumerate(self._models)}
        return [positions[name] for name, _ in hits if name in positions]


def _positive_desc(scores) -> list[int]:
    idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return [i for i in idx if scores[i] > 0]


def _default_embedder() -> object | None:
    from querygate import config

    if not config.EMBEDDINGS_ENABLED:
        return None
    try:
        from querygate.retrieval.embedder import FastEmbedEmbedder

        return FastEmbedEmbedder(config.EMBEDDING_MODEL)
    except Exception as exc:
        _log.warning("could not load embedder %s: %s", config.EMBEDDING_MODEL, exc)
        return None

"""Embedder protocol plus the default fastembed (ONNX) implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import numpy as np


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray: ...


class FastEmbedEmbedder:
    """Local ONNX embeddings via fastembed, no torch. Bake the model into the
    image (fixed cache dir) to avoid a cold-start download in production."""

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name)

    def embed(self, texts: list[str]) -> np.ndarray:
        import numpy as np

        return np.array(list(self._model.embed(texts)), dtype="float32")

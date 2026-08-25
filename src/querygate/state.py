"""Process-global mutable state: catalog, index, build bookkeeping."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from querygate.catalog.models import SemanticCatalog
    from querygate.retrieval.index import CatalogIndex


class AppState:
    def __init__(self) -> None:
        self.catalog: SemanticCatalog | None = None
        self.index: CatalogIndex | None = None
        self.catalog_loaded_at: float = 0.0
        self.build_lock = threading.Lock()
        self.build_in_flight = False


state = AppState()

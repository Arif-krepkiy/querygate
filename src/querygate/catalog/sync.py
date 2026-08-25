"""Catalog loading, source precedence, TTL refresh and background warm."""

from __future__ import annotations

import threading
import time

from querygate import config
from querygate.catalog.loaders import bundle, dbt
from querygate.catalog.models import CatalogError, SemanticCatalog
from querygate.log_setup import get_logger
from querygate.obs import metrics
from querygate.retrieval.index import CatalogIndex
from querygate.state import state

_log = get_logger("querygate.catalog.sync")


class CatalogNotReadyError(RuntimeError):
    """Raised when a tool needs the catalog but the first build is not done."""


def _load_catalog() -> SemanticCatalog:
    if config.CATALOG_S3_URI:
        from querygate.catalog import storage

        target = storage.download(config.CATALOG_S3_URI)
        return dbt.compile_dbt_target(target, config.ALLOWED_SCHEMAS)
    if config.CATALOG_DBT_TARGET_PATH:
        return dbt.compile_dbt_target(config.CATALOG_DBT_TARGET_PATH, config.ALLOWED_SCHEMAS)
    return bundle.load_bundle(config.CATALOG_LOCAL_PATH)


def _build() -> None:
    try:
        catalog = _load_catalog()
        index = CatalogIndex.build(catalog)
    except (CatalogError, OSError) as exc:
        if state.catalog is None:
            _log.error("initial catalog build failed: %s", exc)
        else:
            _log.warning("catalog refresh failed, keeping last-known-good: %s", exc)
        return
    state.catalog = catalog
    state.index = index
    state.catalog_loaded_at = time.time()
    metrics.record_catalog(len(catalog.models), state.catalog_loaded_at)
    _log.info("catalog ready: %d models (build=%s)", len(catalog.models), catalog.build.get("git_sha", "?"))


def _stale() -> bool:
    return (time.time() - state.catalog_loaded_at) > config.CATALOG_REFRESH_TTL_SECONDS


def request_reload(*, force: bool = False) -> bool:
    """Kick a background (re)build unless one is already in flight.

    Returns True if this call started a build, False if one was already running.
    """
    with state.build_lock:
        if state.build_in_flight:
            return False
        if not force and state.catalog is not None and not _stale():
            return False
        state.build_in_flight = True

    def _run() -> None:
        try:
            _build()
        finally:
            with state.build_lock:
                state.build_in_flight = False

    threading.Thread(target=_run, name="qg-catalog-build", daemon=True).start()
    return True


def ensure_warm() -> None:
    """Trigger a build if the catalog is missing or stale. Non-blocking."""
    if state.catalog is None or _stale():
        request_reload()


def is_ready() -> bool:
    return state.catalog is not None and state.index is not None


def get_catalog() -> SemanticCatalog:
    ensure_warm()
    if state.catalog is None:
        msg = "Catalog is warming up. Retry in a few seconds."
        raise CatalogNotReadyError(msg)
    return state.catalog


def get_index() -> CatalogIndex:
    ensure_warm()
    if state.index is None:
        msg = "Search index is warming up. Retry in a few seconds."
        raise CatalogNotReadyError(msg)
    return state.index

"""Shared fixtures: the bundled sample catalog."""

from __future__ import annotations

import pytest

from querygate import config
from querygate.catalog.loaders import bundle
from querygate.catalog.models import SemanticCatalog


@pytest.fixture(scope="session")
def catalog() -> SemanticCatalog:
    return bundle.load_bundle(config.CATALOG_LOCAL_PATH)

"""Download dbt artifacts from an S3-compatible bucket (AWS S3 or MinIO)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import urlparse

from querygate import config
from querygate.catalog.models import CatalogError
from querygate.log_setup import get_logger

_log = get_logger("querygate.catalog.storage")

_ARTIFACTS = ("manifest.json", "catalog.json", "glossary.json")


def download(uri: str) -> str:
    """Fetch the artifact set under ``s3://bucket/prefix/`` into a temp dir."""
    try:
        import boto3
    except ImportError as exc:
        msg = "boto3 is required for S3 catalog sync. Install querygate[s3]."
        raise CatalogError(msg) from exc

    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        msg = f"Expected an s3:// URI, got '{uri}'."
        raise CatalogError(msg)
    bucket = parsed.netloc
    prefix = parsed.path.lstrip("/").rstrip("/")

    client = boto3.client("s3", endpoint_url=config.CATALOG_S3_ENDPOINT)
    dest = Path(tempfile.mkdtemp(prefix="qg-catalog-"))
    fetched = 0
    for name in _ARTIFACTS:
        key = f"{prefix}/{name}" if prefix else name
        try:
            client.download_file(bucket, key, str(dest / name))
            fetched += 1
        except Exception as exc:
            if name in ("manifest.json", "catalog.json"):
                msg = f"Required artifact '{key}' could not be downloaded: {exc}"
                raise CatalogError(msg) from exc
            _log.info("optional artifact %s absent, skipping", key)
    _log.info("downloaded %d artifacts from %s", fetched, uri)
    return str(dest)

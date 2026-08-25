"""Opaque pagination cursors."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json

from querygate.query.validation import GroundingError


def _fingerprint(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()[:16]


def encode(sql: str, next_offset: int) -> str:
    payload = json.dumps({"o": next_offset, "q": _fingerprint(sql)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode(cursor: str, sql: str) -> int:
    """Return the offset this cursor points at, for this exact query."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        offset = int(payload["o"])
        fingerprint = str(payload["q"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        msg = "Malformed cursor. Omit it to start from the first page."
        raise GroundingError(msg) from exc

    if fingerprint != _fingerprint(sql):
        msg = (
            "This cursor belongs to a different query. Re-send the SQL exactly as it was "
            "on the first page, or omit the cursor to start over."
        )
        raise GroundingError(msg)
    if offset < 0:
        msg = "Cursor offset must not be negative."
        raise GroundingError(msg)
    return offset

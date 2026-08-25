from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec

from querygate.catalog.models import CatalogError
from querygate.catalog.sync import CatalogNotReadyError
from querygate.query.certification import CertificationError
from querygate.query.governance import GovernanceError
from querygate.query.identity import IdentityError
from querygate.query.masking import MaskingError
from querygate.query.validation import GroundingError
from querygate.ratelimit import RateLimitError
from querygate.warehouse.types import WarehouseError

_P = ParamSpec("_P")


def compact_json(payload: object) -> str:
    return json.dumps(payload, separators=(",", ":"), default=str)


def error_json(message: str, *, kind: str = "error") -> str:
    return compact_json({"error": message, "kind": kind})


def with_tool_errors(func: Callable[_P, Awaitable[str]]) -> Callable[_P, Awaitable[str]]:
    @wraps(func)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> str:
        try:
            return await func(*args, **kwargs)
        except RateLimitError as exc:
            # retry_after is surfaced so the agent can back off by the right
            # amount instead of retrying immediately and digging deeper.
            return compact_json(
                {"error": str(exc), "kind": "rate_limit", "retry_after": round(exc.retry_after, 1)}
            )
        except CatalogNotReadyError as exc:
            return error_json(str(exc), kind="warming")
        except GovernanceError as exc:
            return error_json(str(exc), kind="governance")
        except IdentityError as exc:
            return error_json(str(exc), kind="identity")
        except CertificationError as exc:
            return error_json(str(exc), kind="certification")
        except MaskingError as exc:
            return error_json(str(exc), kind="masking")
        except GroundingError as exc:
            return error_json(str(exc), kind="grounding")
        except WarehouseError as exc:
            return error_json(str(exc), kind="warehouse")
        except CatalogError as exc:
            return error_json(f"Catalog unavailable: {exc}", kind="catalog")
        except ValueError as exc:
            return error_json(str(exc))

    return wrapper

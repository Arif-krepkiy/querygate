from __future__ import annotations

from importlib.resources import files

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from querygate import config
from querygate.auth.factory import create_verifier
from querygate.catalog.sync import ensure_warm, is_ready
from querygate.log_setup import get_logger
from querygate.obs import metrics as qg_metrics
from querygate.state import state

_log = get_logger("querygate.api")


def _instructions() -> str:
    base = files("querygate.references").joinpath("llm-instructions.md").read_text(encoding="utf-8")
    if config.INCLUDE_PROVENANCE:
        return base + _DEV_ADDENDUM
    return base


_DEV_ADDENDUM = """

## DEV/DEBUG SESSION: technical detail allowed
`QG_INCLUDE_PROVENANCE` is on: a developer is inspecting output. After the
plain-language answer you MAY append a compact technical footer (tables, plan
cost, whether the result was tenant-scoped) from the `provenance` field. This
override applies only to this dev session.
"""


def build_mcp() -> FastMCP:
    issuer = f"http://{config.MCP_HOST}:{config.MCP_PORT}"
    auth_settings = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(f"{issuer}/mcp"),
        required_scopes=[],
    )
    mcp = FastMCP(
        "querygate",
        instructions=_instructions(),
        host=config.MCP_HOST,
        port=config.MCP_PORT,
        auth=auth_settings,
        token_verifier=create_verifier(),
    )
    _register_health(mcp)
    return mcp


def _register_health(mcp: FastMCP) -> None:
    @mcp.custom_route("/healthz", methods=["GET"])
    async def _healthz(_req: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/metrics", methods=["GET"])
    async def _metrics(_req: Request) -> Response:
        if not config.METRICS_ENABLED or not qg_metrics.setup():
            return JSONResponse({"error": "metrics disabled"}, status_code=404)
        payload, content_type = qg_metrics.render()
        return Response(content=payload, media_type=content_type)

    @mcp.custom_route("/readyz", methods=["GET"])
    async def _readyz(_req: Request) -> Response:
        if state.shutting_down:
            return JSONResponse({"status": "draining"}, status_code=503)
        ensure_warm()
        if is_ready():
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "warming"}, status_code=503)

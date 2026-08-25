"""Governed text-to-SQL MCP server."""

from __future__ import annotations


def main() -> None:
    from querygate import obs
    from querygate.api.tools import mcp
    from querygate.catalog.sync import ensure_warm
    from querygate.log_setup import configure, get_logger

    configure()
    obs.setup()
    log = get_logger("querygate")
    ensure_warm()  # kick the first catalog build off the request path
    log.info("QueryGate starting on streamable-http")
    mcp.run(transport="streamable-http")

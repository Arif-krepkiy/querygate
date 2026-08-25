"""Governed text-to-SQL MCP server."""

from __future__ import annotations


def _install_drain_handler() -> None:
    """Fail readiness on SIGTERM so the load balancer stops sending new work.

    Kubernetes sends SIGTERM and only then removes the pod from endpoints, so a
    server that exits immediately drops requests that were already in flight.
    """
    import signal

    from querygate.log_setup import get_logger
    from querygate.state import state

    log = get_logger("querygate")

    def _drain(signum: int, _frame: object) -> None:
        state.shutting_down = True
        log.info("signal %s received: draining, readiness now failing", signum)
        signal.signal(signum, signal.SIG_DFL)

    try:
        signal.signal(signal.SIGTERM, _drain)
        signal.signal(signal.SIGINT, _drain)
    except ValueError:
        # Not the main thread; nothing to install.
        pass


def main() -> None:
    from querygate import obs
    from querygate.api.tools import mcp
    from querygate.catalog.sync import ensure_warm
    from querygate.log_setup import configure, get_logger
    from querygate.query.identity import validate_configuration

    configure()
    obs.setup()
    log = get_logger("querygate")
    validate_configuration()  # refuse to boot on a config that would serve everyone as one identity
    _install_drain_handler()
    ensure_warm()  # kick the first catalog build off the request path
    log.info("QueryGate starting on streamable-http")
    mcp.run(transport="streamable-http")

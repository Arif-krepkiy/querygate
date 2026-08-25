from __future__ import annotations

from mcp.server.auth.provider import AccessToken
from pydantic import ConfigDict

from querygate.auth.principal import Principal


class QGAccessToken(AccessToken):
    """AccessToken carrying the resolved Principal.

    Verifiers return this; ``auth/context.py`` reads the Principal back out
    inside a tool. Identity stays on the request scope and never in process
    state, so one caller's tenant scopes cannot reach another's query.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    principal: Principal

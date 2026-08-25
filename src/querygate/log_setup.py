from __future__ import annotations

import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure() -> None:
    logging.basicConfig(
        level=os.environ.get("QG_LOG_LEVEL", "INFO").upper(),
        format=_FORMAT,
        stream=sys.stderr,
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

"""Logging configuration: one stdout handler, UTC timestamps."""

from __future__ import annotations

import logging
import sys
import time

_MARKER = "_sshport_handler"


def configure_logging(level: str, *, access_log: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)sZ %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    # The formatter's default converter is localtime; override it with gmtime
    # so container logs are unambiguous even if the container timezone changes.
    handler.formatter.converter = time.gmtime  # type: ignore[union-attr]
    setattr(handler, _MARKER, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _MARKER, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    logging.getLogger("uvicorn.access").disabled = not access_log
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    # httpx logs every request at INFO while tests use TestClient.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

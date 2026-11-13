"""Compatibility module for ``uvicorn app.main:create_app --factory``."""

from .web import create_app

__all__ = ["create_app"]

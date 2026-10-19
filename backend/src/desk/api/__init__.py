"""Desk API v1 and the window.DESK compatibility adapter (docs/architecture/05 §2, §4)."""

from .app import create_app, serve

__all__ = ["create_app", "serve"]

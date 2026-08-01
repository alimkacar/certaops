"""Internal FastAPI re-export; public deployments should use ``certaops.api``."""

from __future__ import annotations

from .channels.api import app

__all__ = ["app"]

"""The FillerAI web UI.

A local, dependency-free application over the same engine the CLI uses.
Start it with ``python -m fillerai serve``.
"""

from .server import create_server, serve

__all__ = ["create_server", "serve"]

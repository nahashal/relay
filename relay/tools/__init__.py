"""Tool layer. Import the backend you want; the agent loop only sees ``Backend``."""

from .base import Backend, ToolError
from .mock_backend import MockBackend

__all__ = ["Backend", "ToolError", "MockBackend"]

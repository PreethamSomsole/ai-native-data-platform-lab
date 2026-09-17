"""Model Context Protocol adapter for the AI-native data platform."""

from ai_data_platform.mcp.server import (
    create_default_mcp_server,
    create_mcp_server,
    create_mcp_server_from_paths,
)

__all__ = [
    "create_default_mcp_server",
    "create_mcp_server",
    "create_mcp_server_from_paths",
]

"""
Entry point for MCP server subprocess.

Run with: python -m app.infra_chat_agent.mcp_server
"""
from app.infra_chat_agent.mcp_server.reference_tools_server import main

if __name__ == "__main__":
    main()

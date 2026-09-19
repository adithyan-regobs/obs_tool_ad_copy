"""
EKS Service Onboarding MCP Server Package.

This package contains all components for the EKS service onboarding tool:
- eks_config: Configuration constants
- eks_models: Pydantic models
- eks_generators: File generation functions
- eks_deployment: Kubectl deployment operations
- eks_onboarding_server: Core server logic
- eks_onboarding_sse: FastMCP SSE server wrapper
"""

from app.infra_chat_agent_with_tools.mcp_server.eks.eks_onboarding_sse import create_mcp_server

__all__ = ["create_mcp_server"]

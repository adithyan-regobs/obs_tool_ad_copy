"""
State schema for Service Config Agent LangGraph workflow.

Defines the TypedDict that flows through all nodes in the graph.
"""

from typing import TypedDict, List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.service_config_chat_schemas import (
    ServiceConfigChatContextSchema,
    ServiceMatchSchema,
)


class ServiceConfigAgentState(TypedDict):
    """
    State schema for LangGraph Service Config Agent workflow.

    Flows through: router -> legacy_handler -> (future: agentic nodes)
    """

    # ==================== Input ====================
    user_message: str
    context: ServiceConfigChatContextSchema
    tenants_mst_code: str
    user_mst_code: str

    # ==================== Session ====================
    chat_info_code: str
    is_new_session: bool
    history: List[Dict[str, str]]  # [{"role": "user/agent", "message": "..."}]

    # ==================== Reference Service ====================
    reference_service_code: Optional[str]
    reference_service_name: Optional[str]
    reference_has_config: bool

    # ==================== Intent Detection ====================
    # Route category: "legacy", "agentic", "simple_qa"
    route: str
    # Specific intent within category (e.g., "list_services", "form_fill")
    intent: str

    # ==================== Agentic Workflow ====================
    # Planned tool calls from query planner
    tool_calls: Optional[List[Dict[str, Any]]]
    # Results from tool execution
    tool_results: Optional[List[Dict[str, Any]]]

    # ==================== Response Building ====================
    # Raw response text
    response: str
    # Matched services for list/select intents
    matched_services: Optional[List[ServiceMatchSchema]]
    # Config JSON for form fill
    config_json: Optional[Dict[str, Any]]
    # Single field update for parameter fill
    updation_field: Optional[Dict[str, Any]]
    # Whether config is ready for submission
    is_ready: bool

    # ==================== Database Session ====================
    # Passed through for repository access in nodes
    db: AsyncSession

    # ==================== Error Handling ====================
    error: Optional[str]

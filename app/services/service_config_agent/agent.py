"""
Service Config Agent - LangGraph workflow executor.

Single responsibility: execute the workflow with given state.
Dependencies injected via factory function.

Includes Langfuse workflow tracing for observability.
"""

import asyncio
import time
from typing import Dict, Any, List

from langgraph.graph import StateGraph

from app.services.service_config_agent.state import ServiceConfigAgentState
from app.services.langfuse_service import langfuse_service


class ServiceConfigAgent:
    """
    Executes the service config chat workflow.

    Receives pre-built workflow via dependency injection.
    Single responsibility: orchestrate workflow execution.
    """

    def __init__(self, workflow: StateGraph):
        """
        Initialize agent with workflow.

        Args:
            workflow: Pre-built LangGraph StateGraph
        """
        self._workflow = workflow
        self._app = workflow.compile()

    async def execute(
        self,
        user_message: str,
        context,
        tenants_mst_code: str,
        user_mst_code: str,
        chat_info_code: str,
        is_new_session: bool,
        history: List,
        reference_service_code: str = None,
        reference_has_config: bool = False,
    ) -> Dict[str, Any]:
        """
        Execute the agent workflow.

        Args:
            user_message: User's message
            context: ServiceConfigChatContextSchema
            tenants_mst_code: Tenant code
            user_mst_code: User code
            chat_info_code: Chat session code
            is_new_session: Whether this is a new session
            history: Conversation history
            reference_service_code: Selected reference service code
            reference_has_config: Whether reference service has config

        Returns:
            Response data dict
        """
        start_time = time.perf_counter()
        nodes_executed = []
        success = True
        error_message = None

        initial_state = self._build_initial_state(
            user_message=user_message,
            context=context,
            tenants_mst_code=tenants_mst_code,
            user_mst_code=user_mst_code,
            chat_info_code=chat_info_code,
            is_new_session=is_new_session,
            history=history,
            reference_service_code=reference_service_code,
            reference_has_config=reference_has_config,
        )

        try:
            final_state = await self._app.ainvoke(initial_state)

            # Track nodes from state
            if final_state.get("route"):
                nodes_executed.append(f"router:{final_state['route']}")
            if final_state.get("intent"):
                nodes_executed.append(f"intent:{final_state['intent']}")
            if final_state.get("error"):
                error_message = final_state["error"]
                success = False

            response = self._extract_response(final_state)
        except Exception as e:
            success = False
            error_message = str(e)
            response = {
                "response": "I encountered an error processing your request.",
                "intent": "error",
                "error": str(e),
            }

        # Fire-and-forget: Log workflow execution
        latency_ms = (time.perf_counter() - start_time) * 1000
        asyncio.create_task(langfuse_service.log_workflow_execution(
            workflow_name="service_config_agent",
            chat_info_code=chat_info_code,
            user_id=user_mst_code,
            tenant_id=tenants_mst_code,
            nodes_executed=nodes_executed,
            latency_ms=latency_ms,
            success=success,
            error_message=error_message,
            metadata={
                "user_message_preview": user_message[:100] if user_message else "",
                "is_new_session": is_new_session,
                "environment": context.environment_enum.value if context else None,
            }
        ))

        return response

    def _build_initial_state(
        self,
        user_message: str,
        context,
        tenants_mst_code: str,
        user_mst_code: str,
        chat_info_code: str,
        is_new_session: bool,
        history: List,
        reference_service_code: str,
        reference_has_config: bool,
    ) -> ServiceConfigAgentState:
        """Build initial state for workflow."""
        return {
            "user_message": user_message,
            "context": context,
            "tenants_mst_code": tenants_mst_code,
            "user_mst_code": user_mst_code,
            "chat_info_code": chat_info_code,
            "is_new_session": is_new_session,
            "history": self._normalize_history(history),
            "reference_service_code": reference_service_code,
            "reference_service_name": context.reference_service_name,
            "reference_has_config": reference_has_config,
            "route": "",
            "intent": "",
            "response": "",
            "matched_services": None,
            "config_json": None,
            "updation_field": None,
            "is_ready": False,
            "db": None,
            "error": None,
        }

    def _normalize_history(self, history: List) -> List[Dict[str, str]]:
        """Normalize history to dict format."""
        if not history:
            return []
        return [
            {"role": h.role, "message": h.message}
            for h in history
        ]

    def _extract_response(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Extract response data from final state."""
        return {
            "response": state.get("response", ""),
            "intent": state.get("intent", ""),
            "matched_services": state.get("matched_services"),
            "reference_service_code": state.get("reference_service_code"),
            "reference_service_name": state.get("reference_service_name"),
            "config_json": state.get("config_json"),
            "updation_field": state.get("updation_field"),
            "is_ready": state.get("is_ready", False),
            "error": state.get("error"),
        }

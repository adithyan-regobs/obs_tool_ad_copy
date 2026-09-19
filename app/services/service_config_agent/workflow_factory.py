"""
Workflow Factory for Service Config Agent.

Single responsibility: build and configure LangGraph workflow.
Follows Open/Closed principle - extend by adding new nodes, not modifying existing.
"""

from functools import partial
from typing import Any, Dict

from langgraph.graph import StateGraph, END

from app.services.service_config_agent.state import ServiceConfigAgentState
from app.services.service_config_agent.nodes.router import (
    router_node,
    ROUTE_LEGACY,
    ROUTE_AGENTIC,
    ROUTE_SIMPLE_QA,
)
from app.services.service_config_agent.nodes.legacy_handler import legacy_handler_node
from app.services.service_config_agent.nodes.query_planner import query_planner_node
from app.services.service_config_agent.nodes.tool_executor import tool_executor_node
from app.services.service_config_agent.nodes.synthesizer import synthesizer_node
from app.services.service_config_agent.protocols import (
    IntentDetectorProtocol,
    IntentHandlerProtocol,
)


class WorkflowFactory:
    """
    Factory for building LangGraph workflows.

    Separates graph construction from agent execution.
    """

    @staticmethod
    def create_legacy_workflow(
        intent_detector: IntentDetectorProtocol,
        intent_handler: IntentHandlerProtocol,
    ) -> StateGraph:
        """
        Create the legacy workflow for Phase 1.

        Graph: START -> router -> legacy_handler -> END

        Args:
            intent_detector: Service for detecting intent
            intent_handler: Service for handling intents

        Returns:
            Configured StateGraph (not compiled)
        """
        graph = StateGraph(ServiceConfigAgentState)

        # Bind dependencies to nodes
        bound_router = partial(router_node, intent_detector=intent_detector)
        bound_legacy = partial(legacy_handler_node, intent_handler=intent_handler)

        # Add nodes
        graph.add_node("router", bound_router)
        graph.add_node("legacy_handler", bound_legacy)

        # Define edges
        graph.set_entry_point("router")
        graph.add_edge("router", "legacy_handler")
        graph.add_edge("legacy_handler", END)

        return graph

    @staticmethod
    def create_agentic_workflow(
        route_classifier,
        intent_detector: IntentDetectorProtocol,
        intent_handler: IntentHandlerProtocol,
        query_planner,
        tool_executor,
        synthesizer,
    ) -> StateGraph:
        """
        Create the full agentic workflow with conditional routing.

        Graph:
            START -> router
            router -> legacy_handler (if LEGACY)
            router -> query_planner (if AGENTIC)
            router -> synthesizer (if SIMPLE_QA)
            query_planner -> tool_executor -> synthesizer
            legacy_handler -> END
            synthesizer -> END

        Args:
            route_classifier: Service for route classification
            intent_detector: Service for detecting intent (legacy)
            intent_handler: Service for handling intents (legacy)
            query_planner: Service for planning tool calls
            tool_executor: Service for executing tools
            synthesizer: Service for response synthesis

        Returns:
            Configured StateGraph (not compiled)
        """
        graph = StateGraph(ServiceConfigAgentState)

        # Bind dependencies to nodes
        bound_router = partial(
            router_node,
            route_classifier=route_classifier,
            intent_detector=intent_detector,
        )
        bound_legacy = partial(legacy_handler_node, intent_handler=intent_handler)
        bound_planner = partial(query_planner_node, query_planner=query_planner)
        bound_executor = partial(tool_executor_node, tool_executor=tool_executor)
        bound_synthesizer = partial(synthesizer_node, synthesizer=synthesizer)

        # Add nodes
        graph.add_node("router", bound_router)
        graph.add_node("legacy_handler", bound_legacy)
        graph.add_node("query_planner", bound_planner)
        graph.add_node("tool_executor", bound_executor)
        graph.add_node("synthesizer", bound_synthesizer)

        # Define conditional routing function
        def route_by_category(state: Dict[str, Any]) -> str:
            """Route based on classification result."""
            route = state.get("route", ROUTE_LEGACY)
            if route == ROUTE_AGENTIC:
                return "query_planner"
            else:
                # LEGACY and SIMPLE_QA both go to legacy_handler
                # SIMPLE_QA (greetings, help) is handled via help intent
                return "legacy_handler"

        # Set entry point and conditional edges
        graph.set_entry_point("router")
        graph.add_conditional_edges(
            "router",
            route_by_category,
            {
                "legacy_handler": "legacy_handler",
                "query_planner": "query_planner",
            },
        )

        # Define linear edges for agentic flow
        graph.add_edge("query_planner", "tool_executor")
        graph.add_edge("tool_executor", "synthesizer")

        # Define end edges
        graph.add_edge("legacy_handler", END)
        graph.add_edge("synthesizer", END)

        return graph

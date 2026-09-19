"""
Protocols (interfaces) for Service Config Agent.

Defines contracts for dependency injection following Interface Segregation Principle.
"""

from typing import Protocol, Dict, Any, List, Optional, runtime_checkable


@runtime_checkable
class IntentDetectorProtocol(Protocol):
    """Contract for intent detection."""

    async def detect(
        self,
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]] = None,
    ) -> str:
        """Detect intent from message."""
        ...


@runtime_checkable
class IntentHandlerProtocol(Protocol):
    """Contract for intent handling."""

    async def handle(
        self,
        intent: str,
        message: str,
        context: Any,
        tenants_mst_code: str,
        reference_service_code: Optional[str],
        history: List = None,
    ) -> Dict[str, Any]:
        """Handle intent and return response data."""
        ...


@runtime_checkable
class RouteClassifierProtocol(Protocol):
    """Contract for route classification."""

    async def classify(
        self,
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]] = None,
    ) -> str:
        """Classify message into route category."""
        ...


@runtime_checkable
class QueryPlannerProtocol(Protocol):
    """Contract for query planning."""

    async def plan(
        self,
        query: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Plan tool calls for a query."""
        ...


@runtime_checkable
class ToolExecutorProtocol(Protocol):
    """Contract for tool execution."""

    async def execute(
        self,
        tool_calls: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Execute tool calls and return results."""
        ...


@runtime_checkable
class ResponseSynthesizerProtocol(Protocol):
    """Contract for response synthesis."""

    async def synthesize(
        self,
        query: str,
        tool_results: List[Dict[str, Any]],
    ) -> str:
        """Synthesize tool results into response."""
        ...

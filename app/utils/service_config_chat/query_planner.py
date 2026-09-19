"""
Query Planner for Service Config Agent.

Single responsibility: Plan which tools to call based on user query.
Encapsulates LLM calls for tool selection.
"""

import asyncio
import json
import time
from typing import Dict, Any, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.services.langfuse_service import langfuse_service


QUERY_PLANNER_SYSTEM_PROMPT = """You are a service configuration assistant that plans tool calls.

Your job is to analyze the user's question and decide which tools to call to answer it.

Available Tools:
1. get_service_config(service_name, environment?, geo_loc_code?) - Get full configuration JSON for a service
2. get_parameter_value(service_name, parameter, environment?, geo_loc_code?) - Get a specific parameter value for a service
3. search_services_by_parameter(parameter, environment?, value?, operator?, geo_loc_code?) - Find services matching parameter criteria
   - environment: optional (dev, staging, prod) - defaults to current context environment
   - operator can be: eq, gt, lt, contains, exists
   - geo_loc_code: optional filter by region (e.g., "mumbai", "virginia", "london", "frankfurt")
4. compare_configs(service_a, service_b, parameters?) - Compare configs between two services
5. compare_environments(service_name, env_a, env_b) - Compare same service across environments
6. get_deployment_status(service_name) - Check if a service is deployed
7. get_service_dependencies(service_name) - Get infrastructure dependencies for a service
8. semantic_parameter_search(query) - Get PARAMETER DEFINITION (what a parameter means, valid values, context)
   - Provides helpful context about what a parameter is and how it's used
9. semantic_config_search(query, geo_loc_code?) - Find services similar to a description
   - Use for: "find services like payment-service", "show high-memory services", "services with autoscaling"
   - Returns similar service configs based on semantic similarity
10. get_recommendations(service_type, parameters?, environment?) - Get data-driven parameter recommendations
   - service_type: "API" or "BACKGROUND_SERVICE" (or "worker") - REQUIRED
   - Returns recommended values based on what most similar services use
   - Includes confidence levels (high/medium/low) based on data availability
   - Use when user asks: "what should I use for...", "recommended values for...", "best practices for API services"
11. validate_config(config, service_type) - Validate config values against common patterns
   - config: Dict of parameter values to validate (e.g., {"cpu": 512, "memory": 1024})
   - service_type: "API" or "BACKGROUND_SERVICE"
   - Returns whether values match common usage patterns (optimal/acceptable/unusual)
   - Use when user asks: "is this config good?", "validate my config", "check my settings"
12. get_available_listener_priority(proposed_value?) - Get available listener priority value
   - proposed_value: Optional integer to validate (e.g., user asks "is 100 okay for listener priority?")
   - Returns next available priority if no proposed_value
   - Returns validation result with alternative if proposed_value conflicts
   - Use when user asks: "give me a listener priority", "what priority should I use",
     "is 100 okay for priority?", "can I use 50 for listener?", "listener rule priority value"

Known Geo Locations (use as geo_loc_code, NOT as service names):
- mumbai, virginia, london, frankfurt, oregon, tokyo, sydney, ohio, ireland
- Also: "in X", "for X", "same thing for X" where X is a location = geo_loc_code filter

Rules:
- Only use the tools listed above
- Use the minimum number of tools needed
- Service names can have typos - the tools will fuzzy match them (e.g., "paymnt" → "payment", "usr-api" → "user-api")
- Parameter names can be aliases - the tools will resolve them (e.g., "heap size" → "xmx", "ram" → "memory")

CRITICAL - Service Name Detection:
- If user mentions ANY service-like name (even with typos), ALWAYS use get_parameter_value or get_service_config
- Examples that MUST use get_parameter_value:
  - "cpu for payment-svc" → get_parameter_value(service_name="payment-svc", parameter="cpu") ✓
  - "what is memory for usr-api" → get_parameter_value(service_name="usr-api", parameter="memory") ✓
  - "config for order-service" → get_service_config(service_name="order-service") ✓
- ONLY use search_services_by_parameter when NO service name is mentioned:
  - "what is cpu" (no service) → search_services_by_parameter(parameter="cpu") ✓
  - "which services have autoscaling" → search_services_by_parameter(parameter="autoscaling", value=true) ✓

- For simple parameter lookups with a service, prefer get_parameter_value over get_service_config
- If user mentions a specific region/geo location (mumbai, virginia, london, etc.), pass it as geo_loc_code
- IMPORTANT: For geo location follow-ups like "same thing for london", "what about virginia", "in mumbai":
  - Look at conversation history to find the previous tool/parameter/service used
  - Repeat that tool call with the new geo_loc_code
  - Do NOT treat location names as service names
- IMPORTANT: For environment follow-ups like "what about in staging", "same for prod", "in production", "what about staging environment":
  - Look at conversation history to find the previous tool, parameter, and service_name (if any)
  - Repeat that SAME tool call with the new environment
  - Examples:
    - Previous: get_parameter_value(service_name="baa", parameter="cpu") + "what about staging"
      → get_parameter_value(service_name="baa", parameter="cpu", environment="staging")
    - Previous: search_services_by_parameter(parameter="cpu") + "what about staging environment"
      → search_services_by_parameter(parameter="cpu", environment="staging")
  - Known environments: dev, staging, prod
- IMPORTANT: For service TYPE follow-ups like "what if it's a worker", "same for background service", "what about API services":
  - Look at conversation history - if previous call was get_recommendations, repeat with different service_type
  - Examples:
    - Previous: get_recommendations(service_type="API") + "what if it's a worker service"
      → get_recommendations(service_type="BACKGROUND_SERVICE")
    - Previous: get_recommendations(service_type="BACKGROUND_SERVICE") + "what about API services"
      → get_recommendations(service_type="API")
  - Known service types: API, BACKGROUND_SERVICE (also accepts: worker, background, workers)
- IMPORTANT: If the previous response asked "Please specify" or "Please select" between multiple services, DO NOT assume any service was selected unless the user explicitly names one in their follow-up. If user asks about a parameter without specifying which service, use search_services_by_parameter instead of get_parameter_value.

CRITICAL - Listener Priority Special Handling:
- When user asks for a listener priority VALUE (not definition), ALWAYS use get_available_listener_priority
- Trigger phrases: "give me a listener priority", "listener priority value", "what priority should I use",
  "suggest a listener priority", "available listener priority", "next listener priority"
- Validation phrases: "is X okay for listener priority", "can I use X for listener", "is priority X available"
- Do NOT use semantic_parameter_search or search_services_by_parameter for these - they show stats, not available values
- Examples:
  - "give me a listener priority" → get_available_listener_priority() ✓
  - "is 100 okay for listener priority?" → get_available_listener_priority(proposed_value=100) ✓
  - "what is listener priority" (asking for definition) → semantic_parameter_search + search_services_by_parameter ✓

- For INITIAL parameter questions without a service (e.g., "what is cpu", "tell me about alb"): Call BOTH semantic_parameter_search (for definition) AND search_services_by_parameter (for actual values)
- For FOLLOW-UP parameter questions (e.g., "what about cpu", "and memory?"): Only call search_services_by_parameter since the parameter context was already explained

Output Format:
Respond with ONLY a JSON object (no markdown, no explanation):
{
  "reasoning": "Brief explanation of your plan",
  "tool_calls": [
    {"tool": "tool_name", "args": {"arg1": "value1", "arg2": "value2"}}
  ]
}

If the question cannot be answered with the available tools, respond:
{
  "reasoning": "Explanation of why tools cannot help",
  "tool_calls": []
}
"""

QUERY_PLANNER_USER_PROMPT = """Context:
- Current service: {service_name}
- Environment: {environment}
- Region: {geo_loc}

{history_context}User Question: "{query}"

Plan the tool calls needed to answer this question. If this is a follow-up question (like "how about X" or "and for Y"), refer to the conversation history to understand what parameter/operation the user is asking about."""


class QueryPlanner:
    """
    Plans tool calls to answer user queries.

    Responsibilities:
    - Analyze user query
    - Select appropriate tools
    - Return structured tool call plan
    """

    def __init__(self, llm):
        """
        Initialize with LLM client.

        Args:
            llm: LangChain chat client for planning
        """
        self.llm = llm

    async def plan(
        self,
        query: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Plan tool calls for a user query.

        Args:
            query: User's question
            context: Service context (service_name, environment, geo_loc)
            history: Conversation history (for context)

        Returns:
            Dict with "reasoning" and "tool_calls" list
        """
        history = history or []

        # Format history for context
        history_context = ""
        if history:
            recent_history = history[-4:]  # Last 2 exchanges
            history_lines = []
            for msg in recent_history:
                role = msg.get("role", "user")
                message = msg.get("message", "")[:200]

                # Flag unresolved clarification requests
                if role == "agent" and any(phrase in message.lower() for phrase in [
                    "please specify", "please select", "multiple services match",
                    "did you mean", "which one"
                ]):
                    message = f"[UNRESOLVED CLARIFICATION - user did not select] {message}"

                history_lines.append(f"  {role}: {message}")
            history_context = "Recent conversation:\n" + "\n".join(history_lines) + "\n\n"

        # Build prompt
        user_prompt = QUERY_PLANNER_USER_PROMPT.format(
            service_name=context.get("service_name", "not selected"),
            environment=context.get("environment", "not specified"),
            geo_loc=context.get("geo_loc", "not specified"),
            history_context=history_context,
            query=query,
        )

        messages = [
            SystemMessage(content=QUERY_PLANNER_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        start_time = time.perf_counter()
        response = await self.llm.ainvoke(messages)
        latency_ms = (time.perf_counter() - start_time) * 1000

        # Parse response
        plan = self._parse_response(response.content)

        # Fire-and-forget: Log LLM call
        asyncio.create_task(langfuse_service.log_llm_call(
            call_type="query_planning",
            input_text=query[:200],
            output_text=json.dumps(plan)[:500],
            latency_ms=latency_ms,
            metadata={
                "tool_count": len(plan.get("tool_calls", [])),
            }
        ))

        return plan

    def _parse_response(self, content: str) -> Dict[str, Any]:
        """Parse LLM response to structured plan."""
        content = content.strip()

        # Remove markdown code blocks if present
        if content.startswith("```"):
            lines = content.split("\n")
            # Remove first and last lines (```json and ```)
            content = "\n".join(lines[1:-1])

        try:
            plan = json.loads(content)

            # Validate structure
            if "tool_calls" not in plan:
                plan["tool_calls"] = []
            if "reasoning" not in plan:
                plan["reasoning"] = ""

            # Validate each tool call
            valid_tools = {
                "get_service_config",
                "get_parameter_value",
                "search_services_by_parameter",
                "compare_configs",
                "compare_environments",
                "get_deployment_status",
                "get_service_dependencies",
                "semantic_parameter_search",
                "semantic_config_search",
                "get_recommendations",
                "validate_config",
                "get_available_listener_priority",
            }

            validated_calls = []
            for call in plan["tool_calls"]:
                if call.get("tool") in valid_tools:
                    validated_calls.append(call)

            plan["tool_calls"] = validated_calls
            return plan

        except json.JSONDecodeError:
            return {
                "reasoning": "Failed to parse planning response",
                "tool_calls": [],
                "error": "parse_error",
            }

import json
import logging
from langgraph.graph import StateGraph, START, END
from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.infra_chat_constants import InfraChatConstants
from app.infra_chat_agent import checkpointer as checkpointer_module

logger = logging.getLogger(__name__)

from app.infra_chat_agent.init_node import init_node
from app.infra_chat_agent.intent_detector_deterministic_node import intent_detector_deterministic_node
from app.infra_chat_agent.intent_detector_llm_pre_checks_node import intent_detector_llm_pre_checks_node
from app.infra_chat_agent.intent_detector_llm_node import intent_detector_llm_node
from app.infra_chat_agent.guardrails.input_guardrail_node import input_guardrail_node
from app.infra_chat_agent.guardrails.output_guardrail_node import output_guardrail_node
from app.infra_chat_agent.rbac_node import rbac_node
from app.infra_chat_agent.switch_intent_node import switch_intent_node
from app.infra_chat_agent.switch_confirmation_node import switch_confirmation_node
from app.infra_chat_agent.placement_param_validator_node import placement_param_validator_node
from app.infra_chat_agent.workflows.create.parameter_extraction_deterministic_node import parameter_extraction_deterministic_node
from app.infra_chat_agent.workflows.create.parameter_extraction_llm_node import parameter_extraction_llm_node
from app.infra_chat_agent.workflows.create.policy_validator_node import policy_validator_node
from app.infra_chat_agent.workflows.create.response_handler_node import response_handler_node
from app.infra_chat_agent.workflows.create.create_flow_v2_node import create_flow_v2_node
from app.infra_chat_agent.workflows.create.db_user_management_node import db_user_management_node
from app.infra_chat_agent.workflows.create.create_infra_v3_node import create_infra_v3_node
from app.infra_chat_agent.workflows.reference.reference_node import create_reference_node
from app.infra_chat_agent.workflows.reference.logging_tool_node import create_logging_tool_node
from app.infra_chat_agent.config.mcp_tool_provider import get_mcp_tools, build_resource_tools_map
from app.infra_chat_agent.workflows.qa.qa_execution_node import qa_execution_node
from app.infra_chat_agent.workflows.unsupported.unsupported_execution_node import unsupported_execution_node


def _normalize_create_resource(resource: str) -> str:
    """Normalize short CREATE resource aliases to canonical infra type codes."""
    normalized = (resource or "").strip().lower()
    alias_map = {
        "s3": "s3_infrastructuretype_ref",
        "sqs": "sqs_infrastructuretype_ref",
        "dynamodb": "dynamodb_infrastructuretype_ref",
        "database": "database_infrastructuretype_ref",
        "database_user": "database_user_infrastructuretype_ref",
        "kong_gateway": "kong_gateway_infrastructuretype_ref",
        "kong_route": "kong_gateway_infrastructuretype_ref",
    }
    return alias_map.get(normalized, resource or "")


async def infra_chat_graph():
    """
    Build and compile the infra chat graph.

    This function is async because MCP tools initialization requires async context.
    """
    # Get MCP tools (singleton, lazy initialization)
    tools = await get_mcp_tools()

    # Create reference node with bound tools
    reference_node = create_reference_node(tools)

    # Build resource → filtered tools mapping and create create_flow_v2 node
    resource_tools_map = build_resource_tools_map(tools)
    create_flow_v2 = create_flow_v2_node(resource_tools_map)

    # Database user management tools (inclusive filter)
    DB_USER_MANAGEMENT_TOOL_NAMES = {
        "list_database_servers",
        "list_databases_for_server",
        "get_database_users_with_grants",
        "structure_mysql_database_user_grants",
        "structure_psql_database_user_grants",
        "finalize_database_user_creation",
    }
    db_user_management_tools = [t for t in tools if t.name in DB_USER_MANAGEMENT_TOOL_NAMES]

    # Create db_user_management node with filtered tools
    db_user_management_node_fn = db_user_management_node(db_user_management_tools)

    # # S3 creation tools (dedicated node) — commented out, S3 now uses create_flow_v2
    # S3_CREATION_TOOL_NAMES = {"create_s3_bucket"}
    # s3_creation_tools = [t for t in tools if t.name in S3_CREATION_TOOL_NAMES]
    # create_infra_v3_node_fn = create_infra_v3_node(s3_creation_tools)

     # Create tool execution node with logging wrapper
    tool_node = create_logging_tool_node(tools)

    graph: StateGraph = StateGraph(ChatState)

    graph.add_node("init_node", init_node)
    graph.add_node("intent_detector_deterministic_node", intent_detector_deterministic_node)
    graph.add_node("input_guardrail_node", input_guardrail_node)
    graph.add_node("output_guardrail_node", output_guardrail_node)
    graph.add_node("intent_detector_llm_pre_checks_node", intent_detector_llm_pre_checks_node)
    graph.add_node("intent_detector_llm_node", intent_detector_llm_node)
    graph.add_node("rbac_node", rbac_node)
    graph.add_node("switch_intent_node", switch_intent_node)
    graph.add_node("switch_confirmation_node", switch_confirmation_node)
    graph.add_node("placement_param_validator_node", placement_param_validator_node)
    graph.add_node("parameter_extraction_deterministic_node", parameter_extraction_deterministic_node)
    graph.add_node("parameter_extraction_llm_node", parameter_extraction_llm_node)
    graph.add_node("policy_validator_node", policy_validator_node)
    graph.add_node("response_handler_node", response_handler_node)
    graph.add_node("reference_node", reference_node)
    graph.add_node("create_flow_v2", create_flow_v2)  # V2 CREATE flow with MCP tools
    graph.add_node("db_user_management_node", db_user_management_node_fn)  # Database user management flow
    # graph.add_node("create_infra_v3_node", create_infra_v3_node_fn)  # S3 creation (dedicated node) — S3 now uses create_flow_v2
    graph.add_node("tool_node", tool_node)
    graph.add_node("qa_execution_node", qa_execution_node)
    graph.add_node("unsupported_execution_node", unsupported_execution_node)

    # Entry point: START to init_node
    graph.add_edge(START, "init_node")

    # Init connects to input guardrails before intent detection.
    graph.add_edge("init_node", "input_guardrail_node")

    # Route based on guardrail status
    def edge_from_input_guardrails(state: dict) -> str:
        if state.get("guardrail_status") == "input_blocked":
            return "guardrail_blocked"
        return "guardrail_passed"

    graph.add_conditional_edges(
        "input_guardrail_node",
        edge_from_input_guardrails,
        {
            "guardrail_blocked": "response_handler_node",
            "guardrail_passed": "intent_detector_deterministic_node",
        }
    )

    # Method to check whether to use llm fallback.
    def edge_from_deterministic_extractor(state: dict) -> str:
        intent_status = state.get("intent_detection_method_status", InfraChatConstants.LLM_INTENT_DETECTION_REQUIRED)
        # Check if intent detection success. Other wise llm fallback.
        if(intent_status == InfraChatConstants.DETERMINISTIC_INTENT_DETECTION_SUCCESS):
            return InfraChatConstants.DETERMINISTIC_INTENT_DETECTION_SUCCESS
        return InfraChatConstants.LLM_INTENT_DETECTION_REQUIRED

    #From deterministic conditionally route to llm node.
    graph.add_conditional_edges(
        "intent_detector_deterministic_node",
        edge_from_deterministic_extractor,
        {
            InfraChatConstants.LLM_INTENT_DETECTION_REQUIRED: "intent_detector_llm_pre_checks_node",
            InfraChatConstants.DETERMINISTIC_INTENT_DETECTION_SUCCESS: "rbac_node",
        }
    )

    #From pre_checks to llm node
    graph.add_edge("intent_detector_llm_pre_checks_node", "intent_detector_llm_node")

    #From intent llm node to rbac
    graph.add_edge("intent_detector_llm_node", "rbac_node")

    #From rbac to switch_intent
    graph.add_edge("rbac_node", "switch_intent_node")

    #From switch_intent, route based on intent and pending_switch
    def edge_from_switch_intent(state: dict) -> str:
        pending_switch = state.get("pending_switch")
        switch_status = state.get("switch_intent_status", "")

        # If this is a RESUME operation, route to response_handler to show resuming message
        if switch_status == InfraChatConstants.SWITCH_INTENT_RESUMING:
            return "response_handler"

        # If this is a BLOCKED operation (CREATE → CREATE), route to response_handler to show blocking message
        if switch_status == "switch_blocked":
            return "response_handler"

        # If this is a NEW switch confirmation request, route to response_handler to show message
        if pending_switch and switch_status == InfraChatConstants.SWITCH_INTENT_DETECTED:
            return "response_handler"

        # If user is responding to existing confirmation, route to confirmation node
        # Check for pending_switch being a dict (truthy)
        if pending_switch:
            return "confirm_switch"

        # Otherwise, route based on intent type
        intent = state.get("turn_intent", "UNSUPPORTED")
        resource = _normalize_create_resource(state.get("turn_resource", ""))

        if intent == "CREATE":
            # S3/SQS/DynamoDB/Kong/Database bypass placement validator — routed to create_flow_v2
            if resource in (
                "s3_infrastructuretype_ref",
                "sqs_infrastructuretype_ref",
                "dynamodb_infrastructuretype_ref",
                "kong_gateway_infrastructuretype_ref",
                "database_infrastructuretype_ref",
            ):
                return "create_flow_v2_direct"
            return "create_flow"

        elif intent == "REFERENCE":
            return "reference_flow"
        elif intent == "QA":
            return "qa_flow"
        else:  # UNSUPPORTED or any other
            return "unsupported_flow"

    #From switch_intent node, route to appropriate flow
    graph.add_conditional_edges(
        "switch_intent_node",
        edge_from_switch_intent,
        {
            "response_handler": "response_handler_node",  # Show switch confirmation message
            "confirm_switch": "switch_confirmation_node",  # Handle switch confirmation response
            "create_flow": "placement_param_validator_node",  # Route to placement param validator for CREATE (v1)
            "create_flow_v2_direct": "create_flow_v2",  # S3/SQS/DynamoDB bypass placement validator — tool handles validation
            "reference_flow": "reference_node",  # Route to reference node (LLM + tools)
            "qa_flow": "qa_execution_node",  # Route to QA execution node
            "unsupported_flow": "unsupported_execution_node",  # Route to unsupported execution node
        }
    )

    #From switch_confirmation, route to appropriate workflow or END
    def edge_from_switch_confirmation(state: dict) -> str:
        # If pending_switch still exists, user didn't answer yes/no, stay in confirmation
        if state.get("pending_switch"):
            return END  # Show the confirmation message again

        # Otherwise, route based on the new intent
        # Check if state_hint has the new workflow info
        state_hint = state.get("state_hint") or {}
        running_intent = state_hint.get("running_intent", "")
        running_resource = _normalize_create_resource(state_hint.get("running_resource", ""))

        if running_intent == "CREATE":
            # S3/SQS/DynamoDB/Kong/Database bypass placement validator — tool handles validation
            if running_resource in (
                "s3_infrastructuretype_ref",
                "sqs_infrastructuretype_ref",
                "dynamodb_infrastructuretype_ref",
                "kong_gateway_infrastructuretype_ref",
                "database_infrastructuretype_ref",
            ):
                return "create_flow_v2_direct"
            return "create_flow"
        elif running_intent == "REFERENCE":
            return "reference_flow"
        else:
            return END  # User rejected switch, current workflow continues via response_handler

    graph.add_conditional_edges(
        "switch_confirmation_node",
        edge_from_switch_confirmation,
        {
            "create_flow": "placement_param_validator_node",
            "create_flow_v2_direct": "create_flow_v2",  # S3/SQS/DynamoDB bypass placement validator
            "reference_flow": "reference_node",
            END: END
        }
    )

    #From placement_param_validator, route based on whether placement params are complete
    def edge_from_placement_validator(state: dict) -> str:
        remaining_placement = state.get("remaining_placement_parameters", {})
        state_hint = state.get("state_hint") or {}
        resource = _normalize_create_resource(
            state.get("turn_resource", "") or state_hint.get("running_resource", "")
        )
        # S3/SQS/DynamoDB/Kong/Database should never go through placement-validator prompts in Slack flow.
        if resource in (
            "s3_infrastructuretype_ref",
            "sqs_infrastructuretype_ref",
            "dynamodb_infrastructuretype_ref",
            "kong_gateway_infrastructuretype_ref",
            "database_infrastructuretype_ref",
        ):
            return "placement_params_complete_for_create_flow_v2"
        # If pre-validation failed, route to response handler to show error to user
        if state.get("turn_requires_user_input"):
            return "missing_placement_params"

        # If there are missing placement parameters, route to response handler
        if remaining_placement:
            return "missing_placement_params"

        if resource == "database_user_infrastructuretype_ref":
            return "placement_params_complete_for_db_user_management_node"
        elif resource in ("database_infrastructuretype_ref", "dynamodb_infrastructuretype_ref"):
            return "placement_params_complete_for_create_flow_v2"
        # Otherwise, proceed to parameter extraction
        return "placement_params_complete"

    graph.add_conditional_edges(
        "placement_param_validator_node",
        edge_from_placement_validator,
        {
            "missing_placement_params": "response_handler_node",  # Ask user for missing placement params
            "placement_params_complete": "parameter_extraction_deterministic_node",  # Continue to attribute extraction
            "placement_params_complete_for_db_user_management_node": "db_user_management_node",  # Continue to attribute extraction
            "placement_params_complete_for_create_flow_v2": "create_flow_v2",  # Continue to attribute extraction
        }
    )

    # Method to check whether to use llm for parameter extraction
    def edge_from_param_deterministic(state: dict) -> str:
        param_method = state.get("parameter_extraction_method", InfraChatConstants.LLM_PARAM_EXTRACTION_REQUIRED)
        # Check if deterministic extraction succeeded
        if param_method == InfraChatConstants.DETERMINISTIC_PARAM_EXTRACTION_SUCCESS:
            return InfraChatConstants.DETERMINISTIC_PARAM_EXTRACTION_SUCCESS
        return InfraChatConstants.LLM_PARAM_EXTRACTION_REQUIRED

    #From parameter extraction deterministic, conditionally route to llm or END
    graph.add_conditional_edges(
        "parameter_extraction_deterministic_node",
        edge_from_param_deterministic,
        {
            InfraChatConstants.LLM_PARAM_EXTRACTION_REQUIRED: "parameter_extraction_llm_node",
            InfraChatConstants.DETERMINISTIC_PARAM_EXTRACTION_SUCCESS: END,  # All parameters collected
        }
    )

    #From parameter extraction llm node to policy validator
    graph.add_edge("parameter_extraction_llm_node", "policy_validator_node")

    #From policy validator to response handler
    graph.add_edge("policy_validator_node", "response_handler_node")

    # Reference workflow: tool loop pattern
    # reference_node (LLM) → tool_node (execute) → reference_node (continue) OR response_handler (done)
    def should_continue_tools(state: dict) -> str:
        """Check if LLM wants to call more tools or is done."""
        from langchain_core.messages import HumanMessage, AIMessage
        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            # If last message has tool_calls, route to tool_node
            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                # Guard: detect repeated identical tool calls to prevent infinite loops
                current_calls = [
                    (tc.get("name"), json.dumps(tc.get("args", {}), sort_keys=True))
                    for tc in last_msg.tool_calls
                ]
                repeat_count = 0
                for msg in reversed(messages[:-1]):
                    if isinstance(msg, HumanMessage):
                        break
                    if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                        prev_calls = [
                            (tc.get("name"), json.dumps(tc.get("args", {}), sort_keys=True))
                            for tc in msg.tool_calls
                        ]
                        if prev_calls == current_calls:
                            repeat_count += 1
                        else:
                            break
                if repeat_count >= 1:
                    logger.warning(
                        "[should_continue_tools] Detected repeated identical tool call "
                        f"({current_calls[0][0]}) {repeat_count + 1} times — "
                        "breaking loop and routing to response"
                    )
                    return "response"
                return "tools"
        # Otherwise, LLM is done - route to response handler
        return "response"

    graph.add_conditional_edges(
        "reference_node",
        should_continue_tools,
        {
            "tools": "tool_node",  # Execute tool calls
            "response": "response_handler_node",  # LLM finished, generate response
        }
    )

    # After tool execution, route back to the appropriate LLM node based on context
    def route_from_tool_node(state: dict) -> str:
        """Route tool results back to the correct LLM node (reference, create_flow_v2, or db_user_management_node)."""
        # Check state_hint to determine which workflow is active
        state_hint = state.get("state_hint") or {}
        running_resource = _normalize_create_resource(state_hint.get("running_resource", ""))

        # Route based on running_resource type
        if running_resource == "database_user_infrastructuretype_ref":
            return "db_user_management_node"
        elif running_resource in ("s3_infrastructuretype_ref", "database_infrastructuretype_ref", "sqs_infrastructuretype_ref", "dynamodb_infrastructuretype_ref", "kong_gateway_infrastructuretype_ref"):
            return "create_flow_v2"

        # Default: route to reference_node (for REFERENCE workflows)
        return "reference_node"

    graph.add_conditional_edges(
        "tool_node",
        route_from_tool_node,
        {
            "reference_node": "reference_node",
            "create_flow_v2": "create_flow_v2",
            "db_user_management_node": "db_user_management_node",
        }
    )

    # Create Flow V2 workflow: tool loop pattern (same as reference)
    # create_flow_v2 (LLM) → tool_node (execute) → create_flow_v2 (continue) OR response_handler (done)
    def should_continue_tools_v2(state: dict) -> str:
        """Check if LLM wants to call more tools or is done (for create_flow_v2)."""
        from langchain_core.messages import ToolMessage, HumanMessage, AIMessage

        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:

                # Guard 1: check if the most recent ToolMessage (within the current
                # turn) already returned is_ready=true.  This prevents infinite
                # loops where the LLM keeps calling the same creation tool after
                # it already succeeded.
                #
                # Only check ToolMessages AFTER the last HumanMessage — an
                # is_ready=true from a previous turn should NOT block a new tool
                # call in the current turn (e.g., user updating a parameter).
                for msg in reversed(messages[:-1]):
                    if isinstance(msg, HumanMessage):
                        break
                    if isinstance(msg, ToolMessage):
                        try:
                            content = msg.content
                            if isinstance(content, list):
                                content = content[0].get("text", "") if content else ""
                            result = json.loads(content)
                            if result.get("is_ready") is True:
                                logger.warning(
                                    "[should_continue_tools_v2] Tool already returned "
                                    "is_ready=true but LLM issued new tool_calls — "
                                    "breaking loop and routing to response"
                                )
                                return "response"
                        except (json.JSONDecodeError, TypeError, AttributeError):
                            pass
                        break  # only check the most recent ToolMessage

                # Guard 2: detect repeated identical tool calls within this turn
                # to prevent infinite loops (e.g. LLM keeps calling ListDatabaseServers
                # with the same args after receiving a successful response).
                current_calls = [
                    (tc.get("name"), json.dumps(tc.get("args", {}), sort_keys=True))
                    for tc in last_msg.tool_calls
                ]
                repeat_count = 0
                for msg in reversed(messages[:-1]):
                    if isinstance(msg, HumanMessage):
                        break
                    if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                        prev_calls = [
                            (tc.get("name"), json.dumps(tc.get("args", {}), sort_keys=True))
                            for tc in msg.tool_calls
                        ]
                        if prev_calls == current_calls:
                            repeat_count += 1
                        else:
                            break
                if repeat_count >= 1:
                    logger.warning(
                        "[should_continue_tools_v2] Detected repeated identical tool call "
                        f"({current_calls[0][0]}) {repeat_count + 1} times — "
                        "breaking loop and routing to response"
                    )
                    return "response"

                return "tools"
        # Otherwise, LLM is done - route to response handler
        return "response"

    graph.add_conditional_edges(
        "create_flow_v2",
        should_continue_tools_v2,
        {
            "tools": "tool_node",  # Execute tool calls
            "response": "response_handler_node",  # LLM finished, generate response
        }
    )

    # # Create Infra V3 workflow — commented out, S3 now uses create_flow_v2
    # def should_continue_tools_create_infra_v3(state: dict) -> str:
    #     """Check if LLM wants to call more tools or is done (for create_infra_v3_node)."""
    #     from langchain_core.messages import ToolMessage, HumanMessage
    #
    #     messages = state.get("messages", [])
    #     if messages:
    #         last_msg = messages[-1]
    #         if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
    #             # Guard: check if the most recent ToolMessage already returned is_ready=true
    #             for msg in reversed(messages[:-1]):
    #                 if isinstance(msg, HumanMessage):
    #                     break
    #                 if isinstance(msg, ToolMessage):
    #                     try:
    #                         content = msg.content
    #                         if isinstance(content, list):
    #                             content = content[0].get("text", "") if content else ""
    #                         result = json.loads(content)
    #                         if result.get("is_ready") is True:
    #                             logger.warning(
    #                                 "[should_continue_tools_create_infra_v3] Tool already returned "
    #                                 "is_ready=true but LLM issued new tool_calls — "
    #                                 "breaking loop and routing to response"
    #                             )
    #                             return "response"
    #                     except (json.JSONDecodeError, TypeError, AttributeError):
    #                         pass
    #                     break
    #             return "tools"
    #     return "response"
    #
    # graph.add_conditional_edges(
    #     "create_infra_v3_node",
    #     should_continue_tools_create_infra_v3,
    #     {
    #         "tools": "tool_node",
    #         "response": "response_handler_node",
    #     }
    # )

    # Database User Management workflow: tool loop pattern (same as create_flow_v2)
    # db_user_management_node (LLM) → tool_node (execute) → db_user_management_node (continue) OR response_handler (done)
    def should_continue_tools_db_user_mgmt(state: dict) -> str:
        """Check if LLM wants to call more tools or is done (for db_user_management_node)."""
        messages = state.get("messages", [])
        if messages:
            last_msg = messages[-1]
            # If last message has tool_calls, route to tool_node
            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                return "tools"
        # Otherwise, LLM is done - route to response handler
        return "response"

    graph.add_conditional_edges(
        "db_user_management_node",
        should_continue_tools_db_user_mgmt,
        {
            "tools": "tool_node",  # Execute tool calls
            "response": "response_handler_node",  # LLM finished, generate response
        }
    )

    #From QA execution to response handler
    graph.add_edge("qa_execution_node", "response_handler_node")

    #From unsupported execution to response handler
    graph.add_edge("unsupported_execution_node", "response_handler_node")

    # From response handler to output guardrails, then END
    graph.add_edge("response_handler_node", "output_guardrail_node")
    graph.add_edge("output_guardrail_node", END)

    # Use the global process-level checkpointer (initialized at app startup)
    # This ensures proper connection pool management and multi-worker safety
    if not checkpointer_module.checkpointer:
        raise RuntimeError(
            "LangGraph checkpointer not initialized. "
            "Ensure init_checkpointer() is called at app startup."
        )

    return graph.compile(checkpointer=checkpointer_module.checkpointer)

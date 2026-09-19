"""
QA execution node for handling general questions about AWS infrastructure.

This node processes user questions and generates helpful responses
using LLM with context about available resources and services.
"""
import logging
from typing import Dict, Any
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.utils.graph_utils import get_tenant_id
from app.infra_chat_agent.utils.llm_integration_util import get_llm_client
from app.infra_chat_agent.utils.workflow_trail_util import append_llm_execution_to_trail
from app.infra_chat_agent.config.tenant_config import TENANT_CONFIG, ResourceType
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.config_models import TenantId
from app.services.langfuse_service import langfuse_service
from app.db.session import AsyncSessionLocal
from app.repository.chat_history_repository import ChatHistoryRepository

logger = logging.getLogger(__name__)

# Common greetings for generic response (no LLM call needed)
GREETINGS = {"hi", "hello", "hey", "howdy", "good morning", "good afternoon", "good evening", "hey there", "hi there", "hello there"}

GREETING_RESPONSE = """Hello! I'm here to help you with infrastructure and services.

Feel free to ask me to create resources, or answer any questions you have.

How can I assist you today?"""


def _build_qa_system_prompt(
    tenant_id: str,
    user_question: str,
    tenant_config
) -> str:
    """Build system prompt for QA responses."""

    # Get available resources from resource_meta_repo
    tenant_resources = resource_meta_repo.list_for_tenant(TenantId(tenant_id))

    # Get available recommendation tools
    available_tools = tenant_config.recommendation_tools

    # Get service list
    service_list = tenant_config.services

    # Build resource descriptions using display names
    resource_descriptions = []
    for infra_type, resource_meta in tenant_resources.items():
        # Use display name for user-friendly prompts
        display_name = resource_meta.infra_display_name

        # Show attributes parameters for description
        params = resource_meta.attributes.parameters
        param_info = [f"  - {p.name}: {p.description or 'N/A'}" for p in params if p.description]
        resource_descriptions.append(
            f"\n{display_name.upper()}:\n" + "\n".join(param_info)
        )

    # Build tool descriptions
    tool_descriptions = []
    for tool in available_tools:
        params = tenant_config.recommendation_tool_parameter_list.get(tool, [])
        tool_descriptions.append(
            f"  - {tool}: {f'requires params: {params}' if params else 'no params required'}"
        )

    return f"""You are a helpful AWS infrastructure assistant.

=== MANDATORY OUTPUT FORMAT ===
BULLET POINTS: Put "<&h>" at the START of each line (it's a prefix, NOT an HTML tag, no closing tag needed)
BOLD TEXT: Wrap text with "<&b>" and "</&b>"

CORRECT:
<&h><&b>Queue</&b>: my-queue
<&h><&b>Type</&b>: FIFO

WRONG (never do this):
• Queue: value
- Queue: value
**Queue**: value
===============================

## AVAILABLE RESOURCES:
Users can create these AWS resources:
{"".join(resource_descriptions)}

## AVAILABLE REFERENCE TOOLS:
Users can query service information using these tools:
{chr(10).join(tool_descriptions)}

## AVAILABLE SERVICES:
{", ".join(service_list[:15])}{'...' if len(service_list) > 15 else ''}

## YOUR ROLE:
Answer user questions about AWS infrastructure, resources, and services.

### GUIDELINES:
1. Be helpful and informative
2. Keep answers concise but complete
3. Provide examples when relevant
4. If the question is about creating resources, explain the process
5. If the question is about querying services, explain available options
6. Use the available resources and services listed above to provide context
7. If unsure, suggest the user ask more specific questions

### EXAMPLES OF GOOD RESPONSES:

Q: "How do I create a FIFO queue?"
A: "To create a FIFO queue in SQS, you can say 'create sqs' and I'll guide you through the parameters.
   You'll need to provide:
   - Environment (e.g., prod, stage)
   - Queue type (FIFO or standard)
   - Queue name (without the .fifo suffix — it is added automatically)"

Q: "What's the difference between SQS and SNS?"
A: "SQS (Simple Queue Service) is a message queue service for decoupling components.
   SNS (Simple Notification Service) is a pub/sub messaging service for broadcasting messages.
   Use SQS for point-to-point messaging and SNS for one-to-many messaging."

Q: "What services can I query?"
A: "You can query information about services using commands like:
   - 'list services' - See all available services
   - 'show config of <service>' - Get service configuration
   - 'what's the cpu/memory of <service>' - Get specific parameters"

### CURRENT USER QUESTION:
{user_question}

Provide a helpful, concise response.
"""


async def qa_execution_node(state: ChatState, config) -> ChatState:
    """
    Process QA intent using LLM to generate helpful responses with conversation context.

    Args:
        state: Current chat state
        config: Graph configuration

    Returns:
        Updated state with turn_user_response containing the answer
    """
    tenant_id: str = get_tenant_id(state)
    user_question: str = (
        state.get("validated_user_message")
        or state.get("user_message", "")
    ).strip()

    if not user_question:
        return {
            "turn_user_response": "How can I help you with AWS infrastructure?"
        }

    # Handle greetings with generic response (no LLM call needed)
    if user_question.lower() in GREETINGS:
        logger.info(f"[QA_EXECUTION] Greeting detected: {user_question}")
        return {
            "turn_user_response": GREETING_RESPONSE
        }

    # Get tenant config
    tenant_cfg = TENANT_CONFIG.get(tenant_id, TENANT_CONFIG["default"])

    # Build system prompt
    system_prompt = _build_qa_system_prompt(
        tenant_id,
        user_question,
        tenant_cfg
    )

    # Load recent QA conversation history (user questions + QA answers only)
    thread_id = config.get("configurable", {}).get("thread_id", "unknown")

    try:
        async with AsyncSessionLocal() as session:
            repo = ChatHistoryRepository(session)

            # Use smart query: filters to user messages + QA answering records only
            history_messages = await repo.get_qa_conversation_history(
                thread_id=thread_id,
                limit=10  # Last 10 messages for context
            )

            # Format QA conversation context
            if history_messages:
                context_lines = ["\n## Recent QA Conversation:"]
                for msg in history_messages:
                    role = "User" if msg.role == "user" else "Assistant"
                    context_lines.append(f"{role}: {msg.message.strip()}")

                # Append context to system prompt
                conversation_context = "\n".join(context_lines)
                system_prompt += f"\n\n{conversation_context}"

                logger.info(f"[QA_EXECUTION] Added {len(history_messages)} QA messages as context")

    except Exception as e:
        logger.warning(f"[QA_EXECUTION] Failed to load QA context: {str(e)}")
        # Continue without context - don't fail the request

    # Get LLM client
    llm: ChatOpenAI = get_llm_client(tenant_id, "QA", temperature=0.7)

    # Get Langfuse callback handler
    user_id = state.get("user_id", "anonymous")
    langfuse_handler = langfuse_service.get_callback_handler(
        trace_name="qa_execution"
    )

    try:
        # Invoke LLM
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_question)
        ]

        # Invoke with callback
        invoke_config = {}
        if langfuse_handler:
            invoke_config["callbacks"] = [langfuse_handler]
            # Add metadata via config (Langfuse v3 pattern)
            invoke_config["metadata"] = {
                "langfuse_user_id": user_id,
                "tenant_id": tenant_id,
                "question": user_question[:100],
            }
        
        llm_response = llm.invoke(messages, config=invoke_config)
        answer = llm_response.content or ""

        logger.info(f"[QA_EXECUTION] Answered question: {user_question[:50]}...")

        # Get existing workflow trail or create new
        workflow_trail = state.get("workflow_trail", [])

        # Append QA execution metadata to trail using utility function
        workflow_trail = append_llm_execution_to_trail(
            workflow_trail=workflow_trail,
            phase="qa_execution",
            llm_purpose="qa_answering",
            llm_client=llm,
            message=answer.strip(),
            reasoning=f"Generated answer for question: {user_question[:100]}",
            confidence=None  # No confidence score for QA
        )

        return {
            "turn_user_response": answer.strip(),
            "workflow_trail": workflow_trail  # Include updated trail in state
        }

    except Exception as e:
        logger.error(f"[QA_EXECUTION] Error generating response: {str(e)}")
        return {
            "turn_user_response": f"I'm sorry, I encountered an error answering your question. Please try again or rephrase your question."
        }

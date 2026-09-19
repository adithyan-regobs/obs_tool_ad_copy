import json
import logging
from typing import Literal, Optional
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from pydantic import BaseModel, Field

from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.chat_state import StateHint
from app.infra_chat_agent.utils.graph_utils import get_tenant_id
from app.infra_chat_agent.utils.workflow_trail_util import append_llm_execution_to_trail
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.prompt_builder import DefaultPromptBuilder
from app.infra_chat_agent.config.config_models import TenantId
from app.infra_chat_agent.config.reference_prompt_builder import (
    default_reference_prompt_builder,
)
from app.integrations.openai_integration import OpenAIIntegration
from app.services.langfuse_service import langfuse_service
from app.db.session import AsyncSessionLocal
from app.repository.chat_history_repository import ChatHistoryRepository

logger = logging.getLogger(__name__)


async def _load_intent_detection_history(thread_id: str) -> str:
    """
    Load full conversation history for intent detection context.

    This helps the intent detector understand follow-up messages
    (e.g., when user provides parameters in response to a question).

    Args:
        thread_id: Thread identifier

    Returns:
        Formatted conversation history string (in chronological order)
    """
    if not thread_id:
        return ""

    try:
        async with AsyncSessionLocal() as session:
            repo = ChatHistoryRepository(session)

            # Get user + agent messages (excludes internal LLM workflow)
            history = await repo.get_user_agent_conversation_display_history(
                thread_id=thread_id,
                limit=None  # Load all messages
            )

            if not history:
                return ""

            lines = ["## Conversation History:"]
            for msg in history:
                role = "User" if msg.role == "user" else "Assistant"
                content = msg.message.strip()
                # Truncate long messages for token efficiency
                if len(content) > 300:
                    content = content[:300] + "..."
                lines.append(f"{role}: {content}")

            logger.info(f"[INTENT_DETECTOR] Loaded {len(history)} messages from history")
            return "\n".join(lines)

    except Exception as e:
        logger.warning(f"[INTENT_DETECTOR] Failed to load history: {e}")
        return ""


# Build resource examples dynamically from resource_meta_repo
def _build_resource_examples() -> str:
    """Get all registered resource types for Field description."""
    resource_types = set()
    for (tenant_id, infra_type), meta in resource_meta_repo._store.items():
        # Use display name for Pydantic field examples (clean, LLM-friendly)
        resource_types.add(meta.infra_display_name)
    return ", ".join(sorted(resource_types))


_RESOURCE_EXAMPLES = _build_resource_examples()


class DetectedIntent(BaseModel):
    """Structured output for intent detection."""
    intent: Literal["CREATE", "QA", "REFERENCE", "UNSUPPORTED"] = Field(
        description="The detected intent type"
    )
    resource_type: Optional[str] = Field(
        default=None,
        description=f"Resource type for CREATE intent ({_RESOURCE_EXAMPLES})"
    )
    tool_name: Optional[str] = Field(
        default=None,
        description="Tool name for REFERENCE intent (e.g., list_services)"
    )
    confidence: float = Field(
        default=0.0,
        description="Confidence score from 0.0 to 1.0"
    )
    reasoning: str = Field(
        default="",
        description="Brief explanation of the intent detection"
    )


def _build_system_prompt(
    state: ChatState,
    state_hint: StateHint,
    history_context: str = "",
) -> str:
    """Build system prompt with tenant-specific context and conversation history."""

    tenant_id = get_tenant_id(state)
    case_code = state.get("case_code", "")

    # Get available resources for this tenant
    tenant_resources = resource_meta_repo.list_for_tenant(TenantId(tenant_id))

    # Build resource descriptions (ONLY attributes group, not placement)
    resource_descriptions = []
    infra_type_mapping = []  # For LLM mapping section
    for infra_type, resource_meta in tenant_resources.items():
        # Use display name for prompt (clean, LLM-friendly)
        display_name = resource_meta.infra_display_name

        # Build mapping: display name → database code
        infra_type_mapping.append(f"  - {display_name.upper()} → {infra_type}")

        # Add case-based mappings (e.g., "ADD ROUTE" → kong_gateway)
        if resource_meta.cases:
            for case in resource_meta.cases[:2]:  # Limit to first 2 cases
                case_phrase = case.replace("_", " ").upper()
                infra_type_mapping.append(f"  - {case_phrase} → {infra_type}")

        # Only show attributes parameters (placement comes from request)
        attributes_params = resource_meta.attributes.parameters
        param_descriptions = []
        for param in attributes_params:
            desc = DefaultPromptBuilder.build_param_description(
                param=param,
                include_hints=True
            )
            param_descriptions.append(f"  - {desc}")

        resource_descriptions.append(
            f"\n{display_name.upper()}:\n" + "\n".join(param_descriptions)
        )

    # Build dynamic example phrases from tenant resources
    create_examples = []
    for infra_type, resource_meta in tenant_resources.items():
        display_name = resource_meta.infra_display_name
        create_examples.append(f'- "create {display_name}"')
        create_examples.append(f'- "new {display_name}"')
        # Bare resource name (user selecting from available resources)
        create_examples.append(f'- "{display_name}" → CREATE with resource_type={infra_type}')

        # Add case-based examples (e.g., "add route" → kong_gateway)
        if resource_meta.cases:
            for case in resource_meta.cases[:2]:  # Limit to first 2 cases
                case_phrase = case.replace("_", " ")
                create_examples.append(f'- "{case_phrase}" → {infra_type}')

    # Add generic attribute examples
    create_examples.extend([
        '- "enable versioning" (providing attribute value)',
        '- "set encryption to true" (providing attribute value)'
    ])

    # Build REFERENCE section from prompt builder
    reference_section = default_reference_prompt_builder.build_intent_detection_prompt_section()

    # Build the base prompt
    prompt = f"""You are an intent detector for an AWS infrastructure assistant.

Your job is to classify user messages into one of three intent types:

## INTENT TYPES:

### 1. CREATE
The user wants to create an AWS resource.

REQUIRED FIELD: resource_type

Available resource types for this tenant:
{"".join(resource_descriptions)}

## RESOURCE TYPE CODES MAPPING:
When you detect a CREATE intent, you MUST return the full infrastructure type code for resource_type:
{chr(10).join(infra_type_mapping)}

IMPORTANT: Return the exact code shown after the → symbol.

Example CREATE phrases:
{chr(10).join(create_examples[:20])}

### 2. QA
The user is asking a question or seeking clarification.

NO REQUIRED FIELDS

Example QA phrases:
- "how do I create a fifo queue"
- "what's the difference between sqs and sns"
- "can you explain versioning"
- "I need help with something"
- "how do I enable encryption"
- "what is versioning"
- "hi"
- "hello"
- "hey there"
- "good morning"

{reference_section}

### 4. UNSUPPORTED
The user's request cannot be understood or is outside the scope of AWS infrastructure management.

NO REQUIRED FIELDS

Example UNSUPPORTED phrases:
- "what's the weather"
- "tell me a joke"
- "who won the world cup"
- Completely unclear or gibberish input

## IMPORTANT RULES:

1. For CREATE: You MUST extract the resource_type. If not specified, infer from context.
2. For QA: No extraction needed - the user is asking for information.
3. For UNSUPPORTED: Use when the request is completely unrelated to AWS infrastructure or cannot be understood.
4. If uncertain between QA and UNSUPPORTED, prefer QA.
5. QUESTION WORDS (how, what, where, when, why, can you, explain, tell me) usually indicate QA, NOT CREATE.
6. Single attribute values (prod, fifo, true, false, my-bucket) without question words indicate CREATE (user providing parameter).
7. GREETINGS (hi, hello, hey, good morning, etc.) should be QA, not CREATE - these are not parameter values.
"""

    # Add case_code context if available (provides hint but doesn't override)
    if case_code:
        prompt += f"""

## CONTEXT FROM CASE_CODE:
User is working on: {case_code}

This is CONTEXT ONLY - it does NOT determine the intent.
Example behaviors with case_code="{case_code}":
- User says "enable versioning" → CREATE (user providing attribute value)
- User says "how do I enable versioning" → QA (user asking question, ignore case_code)
- User says "what is versioning" → QA (user asking question, ignore case_code)

The case_code tells you what resource the user is LIKELY working on, but the actual USER MESSAGE determines the intent.
"""

    # Build ongoing workflow context section
    running_intent = state_hint.get("running_intent", "None")
    running_resource = state_hint.get("running_resource", "None")
    running_phase = state_hint.get("running_phase", "None")

    # Get display name and build rich parameter examples for running resource
    running_resource_display = running_resource
    all_param_examples = []  # Collected examples for all parameters

    if running_resource and running_resource != "None":
        for infra_type, resource_meta in tenant_resources.items():
            if infra_type == running_resource:
                running_resource_display = resource_meta.infra_display_name

                # Build rich parameter examples using DefaultPromptBuilder
                for param in resource_meta.attributes.parameters[:5]:  # Show first 5 params
                    param_examples = DefaultPromptBuilder.build_intent_detection_param_examples(
                        param=param,
                        resource_display_name=running_resource_display,
                        running_resource_code=running_resource
                    )
                    all_param_examples.extend(param_examples)
                break

    # Build parameter examples section if we have examples
    param_examples_section = ""
    concrete_examples = ""
    if all_param_examples:
        concrete_examples = f"""
✅ Examples that should be CREATE (using actual {running_resource_display.upper()} parameters):
{chr(10).join(all_param_examples[:15])}
"""

    # Add workflow-specific context based on running_intent
    if running_intent == "REFERENCE":
        running_tool = state_hint.get("tool_name", "")
        prompt += f"""

## ONGOING REFERENCE WORKFLOW

Current Intent: REFERENCE
Current Tool: {running_tool}

**Look at the conversation history.** The user is in a REFERENCE workflow - continue as REFERENCE unless they explicitly start something new.

Common REFERENCE follow-up patterns:
- User provides parameter values (environment, location) → REFERENCE
- Assistant showed a list of services → User types a service name → REFERENCE (selecting a service to view)
- Any short response that could be a value or selection → REFERENCE

Only switch intent if user clearly says "create X", asks "what/how/why" questions, or explicitly starts a new request.
"""

    elif running_intent == "CREATE":
        # CREATE workflow context
        prompt += f"""

## ONGOING WORKFLOW CONTEXT:
🎯 CRITICAL: The user is currently working on a CREATE workflow!

Running Intent: {running_intent}
Running Resource: **{running_resource_display.upper()}** ← User is actively creating this resource
Running Phase: {running_phase}

### RULES FOR ONGOING CREATE WORKFLOWS:

When Running Intent=CREATE and Running Phase=parameter_collection,confirmation :
✅ **DEFAULT TO CREATE** - Assume the user is providing parameter values for {running_resource_display.upper()}
✅ **Be VERY LENIENT** - Accept values with spaces, typos, informal language, abbreviations, special characters
✅ **PATH-LIKE INPUTS ARE VALUES** - Inputs starting with "/" or "~" are route/path VALUES, not greetings:
   - "/hello" → CREATE (this is a route path, NOT a greeting - the "/" makes it a path)
   - "/hi" → CREATE (path value, not greeting)
   - "/api/users" → CREATE (path value)
   - "~/hello$" → CREATE (route pattern value)

❌ **QA is STILL ALLOWED** - Users can ask questions mid-workflow:
   - Messages with explicit question words: "what is versioning?" / "how do I enable encryption?" → QA
   - **Messages ending with "?" that are NOT valid parameter values** → QA
     * "partition key?" → QA (asking about the parameter)
     * "what's DLQ?" → QA (asking for explanation)
     * "encryption?" → QA (asking about the parameter)
     * "fifo?" → QA (asking what FIFO means)
   - **Exception**: Route patterns ending with "?" are still CREATE values:
     * "~/api/users?" → CREATE (valid route pattern with ? in regex)

{concrete_examples}

**IMPORTANT USER INPUT PATTERNS:**
- Users provide values in MANY formats (spaces, hyphens, underscores, mixed case)
- Users make TYPOS in parameter names (buckeet → bucket, que → queue, tabel → table)
- Users use CASUAL language ("make it...", "enable...", "turn on...")
- Users provide PARTIAL matches or ABBREVIATED names
- Users may provide JUST THE VALUE with no parameter name

✅ **Treat ALL value-like inputs as CREATE intent with {running_resource}**
🔴 **DO NOT classify as UNSUPPORTED just because of spaces, typos, or informal phrasing**

❌ Switch to QA if:
   - User uses question words: "how do I..." / "what is..." / "explain..." / "can you tell me..." → QA
   - User message ends with "?" and is NOT a valid parameter value pattern → QA

When Running Intent=CREATE and Running Phase=confirmation:
✅ **ALL messages continue the CREATE workflow** with resource_type={running_resource}
✅ Confirmation responses: "yes", "confirm", "ok", "proceed", "do it", "go ahead"
✅ Rejection responses: "no", "cancel", "reject", "abort", "stop", "don't"
✅ Parameter updates: any message that modifies or provides parameter values
✅ Any other message → Still CREATE with {running_resource} (user might be correcting/updating)

🔴 DO NOT switch to QA unless the user uses question words OR message ends with "?" (and is not a valid value pattern)
"""

    # Add conversation history context if available
    if history_context:
        prompt += f"""

{history_context}

IMPORTANT: Consider the conversation context above when classifying the current message.
If the assistant just asked for parameters (like environment, location, service name) and
the user's message looks like parameter values, classify as the SAME intent that is ongoing.
For example: If assistant asked "please specify environment and location" and user says
"staging and mumbai", this is REFERENCE (continuing the query), NOT CREATE.
"""

    return prompt


async def intent_detector_llm_node(state: ChatState, config) -> ChatState:
    """
    Detect user intent using LLM with tenant-specific context.

    Updates state with:
    - turn_intent: The detected intent (CREATE/QA/UNSUPPORTED)
    - turn_resource: The resource type for CREATE intents
    - intent_detection_method_status: Success status
    """

    user_message: str = (
        state.get("validated_user_message")
        or state.get("user_message", "")
    ).strip()

    if not user_message:
        return {
            "intent_detection_method_status": "failed",
            "turn_intent": "UNSUPPORTED",
            "turn_resource": None
        }

    state_hint: StateHint = state.get("state_hint", {})
    case_code = state.get("case_code", "")

    # Get thread_id from config for conversation history
    thread_id = config.get("configurable", {}).get("thread_id", "")

    logger.info(f"[INTENT_DETECTOR] User message: '{user_message}'")
    logger.info(f"[INTENT_DETECTOR] case_code: {case_code}")
    logger.info(f"[INTENT_DETECTOR] Running Intent: {state_hint.get('running_intent')}")
    logger.info(f"[INTENT_DETECTOR] Running Resource: {state_hint.get('running_resource')}")

    # Load conversation history for context
    history_context = await _load_intent_detection_history(thread_id)
    logger.info(f"[INTENT_DETECTOR] thread_id: {thread_id}")
    logger.info(f"[INTENT_DETECTOR] history_context loaded: {bool(history_context)}")
    if history_context:
        logger.info(f"[INTENT_DETECTOR] history_context preview: {history_context[:300]}...")

    # Build system prompt with tenant context and case_code
    system_prompt = _build_system_prompt(state, state_hint, history_context)

    # Get LLM client with structured output (gpt-4o-mini for faster/cheaper intent detection)
    llm: ChatOpenAI = OpenAIIntegration.get_chat_client(temperature=0.0, model="gpt-4o")
    structured_llm = llm.with_structured_output(DetectedIntent)

    # Get Langfuse callback handler for this specific LLM call
    tenant_id = get_tenant_id(state)
    user_id = state.get("user_id", "anonymous")
    langfuse_handler = langfuse_service.get_callback_handler(
        trace_name="intent_detection_llm"
    )

    # Prepare messages
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message)
    ]

    # Invoke LLM
    try:
        # Debug logging
        logger.debug("="*80)
        logger.debug("INTENT DETECTOR LLM INPUT")
        logger.debug(f"User message: {user_message}")
        logger.debug(f"case_code: {case_code}")
        logger.debug(f"State hint: {json.dumps(state_hint, indent=2)}")
        logger.debug("-"*80)
        logger.debug(f"System prompt (first 500 chars): {system_prompt[:500]}...")
        logger.debug("="*80)

        # Pass callback handler if available
        invoke_config = {}
        if langfuse_handler:
            invoke_config["callbacks"] = [langfuse_handler]
            # Add metadata via config (Langfuse v3 pattern)
            invoke_config["metadata"] = {
                "langfuse_user_id": user_id,
                "tenant_id": tenant_id,
                "case_code": case_code,
                "running_intent": state_hint.get("running_intent"),
                "running_resource": state_hint.get("running_resource"),
                "running_phase": state_hint.get("running_phase"),
            }
        
        detected: DetectedIntent = await structured_llm.ainvoke(messages, config=invoke_config)

        logger.info(
            f"Intent detected: {detected.intent}, "
            f"resource: {detected.resource_type}, "
            f"confidence: {detected.confidence}, "
            f"reasoning: {detected.reasoning}"
        )

        # Get existing workflow trail or create new
        workflow_trail = state.get("workflow_trail", [])

        # Append intent detection metadata to trail - store raw LLM response as JSON
        workflow_trail = append_llm_execution_to_trail(
            workflow_trail=workflow_trail,
            phase="intent_detection",
            llm_purpose="intent_detection",
            llm_client=llm,
            message=detected.model_dump_json(),  # Store raw LLM response as JSON for future context
            reasoning=detected.reasoning,
            confidence=detected.confidence
        )

        # Map detected intent to state
        result = {
            "intent_detection_method_status": "llm_success",
            "turn_intent": detected.intent,
            "turn_resource": detected.resource_type,
            "tool_name": detected.tool_name,
            "slot_parameters": {
                "confidence": detected.confidence,
                "reasoning": detected.reasoning,
                "tool_name": detected.tool_name,
            },
            "workflow_trail": workflow_trail  # Include updated trail in state
        }

        return result

    except Exception as e:
        # Fallback on error
        logger.error(f"[INTENT_DETECTOR] LLM invocation failed: {str(e)}", exc_info=True)
        return {
            "intent_detection_method_status": f"failed: {str(e)}",
            "turn_intent": "UNSUPPORTED",
            "turn_resource": None,
            "slot_parameters": {
                "confidence": 0.0,
                "reasoning": f"LLM invocation failed: {str(e)}"
            }
        }

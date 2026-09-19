"""
LangGraph Chat Service for Infrastructure Studio chatbot
Implements conversation management with context awareness and summarization
"""
from typing import TypedDict, List, Dict, Optional, Annotated, Any
from operator import add
from sqlalchemy.ext.asyncio import AsyncSession
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from app.repository.chat_info_repository import ChatInfoRepository
from app.repository.chat_message_repository import ChatMessageRepository
from app.repository.chat_summary_repository import ChatSummaryRepository
from app.repository.services_mst_repository import ServicesMstRepository
from app.services.openai_service import OpenAIService
from app.services.langfuse_service import langfuse_service
from app.services.template_service import TemplateService
from app.schemas.chat_schemas import ChatContextSchema
from app.core.config import settings
from app.core.enum import EnvironmentEnum, InfraVendorEnum, ServiceTypeEnum
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
import logging

logger = logging.getLogger(__name__)


# ==================== State Schema ====================
class ChatState(TypedDict):
    """State schema for LangGraph chat workflow"""
    # Input
    user_message: str
    context: ChatContextSchema
    case_code: str
    case_type_code: str

    # Session data
    chat_info_code: str
    is_new_session: bool

    # Conversation history
    conversation_history: List[Dict[str, str]]  # [{"role": "user/agent", "message": "..."}]
    summary: Optional[str]
    total_message_count: int

    # Infrastructure context
    infrastructure_context: str
    selected_service: Optional[Dict[str, any]]  # Cached service data (name, service_type) as dict for serialization

    # LLM response
    ai_response: str
    ai_response_raw: Optional[str]  # Original response with control flags (for DB context)
    terraform_code: Optional[str]
    service_type: Optional[str]  # "s3" or "sqs" or "gateway"
    is_ready: bool  # Whether configuration is ready to generate
    redirect: bool  # Whether to redirect user to form UI (for complex workflows)
    parameters: Optional[Dict[str, Any]]  # Service-specific extracted parameters

    # Error handling
    error: Optional[str]

    # Slack-specific fields (optional, only used when interaction_mode="slack")
    interaction_mode: Optional[str]  # "web" or "slack"
    slack_channel_id: Optional[str]
    slack_thread_ts: Optional[str]
    slack_user_id: Optional[str]
    pending_selection_type: Optional[str]  # e.g., "product", "environment", "geo_loc"
    pending_selection_options: Optional[List[Dict[str, Any]]]  # Options to show in Slack dropdown


# ==================== LangGraph Chat Service ====================
class LangGraphChatService:
    """Main service for handling chat interactions using LangGraph"""

    def __init__(self, session: AsyncSession):
        """Initialize service with database session"""
        self.session = session
        self.chat_info_repo = ChatInfoRepository(session)
        self.chat_message_repo = ChatMessageRepository(session)
        self.chat_summary_repo = ChatSummaryRepository(session)
        self.openai_service = OpenAIService()
        self.message_threshold = settings.chat_message_threshold

        # Build LangGraph workflow
        self.workflow = self._build_workflow()

    # ==================== Helper Methods ====================
    def _format_display_response(
        self,
        response: str,
        service_type: str = None,
        is_ready: bool = False,
        redirect: bool = False
    ) -> str:
        """
        Format response for display by stripping control flags,
        transforming parameter labels to Title Case, and adding footer.

        Args:
            response: Raw LLM response with control flags
            service_type: Extracted service type (s3, sqs, gateway, dynamodb, etc.)
            is_ready: Whether configuration is ready
            redirect: Whether redirect flag was set

        Returns:
            Cleaned response with info footer
        """
        # Control flags to strip completely (case-insensitive)
        control_flags = [
            'SERVICE_TYPE:',
            'READY:',
            'REDIRECT:',
        ]

        # Parameter labels to transform to Title Case (ALL_CAPS: → Title Case:)
        param_labels = [
            'IDENTIFIER:',
            'METHOD:',
            'ROUTE:',
            'CREATE_DLQ:',
            'FIFO_QUEUE:',
            'VISIBILITY_TIMEOUT_SECONDS:',
            'MAX_RECEIVE_COUNT:',
            'MESSAGE_RETENTION_SECONDS:',
            'DLQ_MESSAGE_RETENTION_SECONDS:',
            'CROSS_ACCOUNT_IDS:',
            'PARTITION_KEY:',
            'PARTITION_KEY_TYPE:',
            'VERSIONING:',
            'ENABLE_S3_REPLICATION:',
            'CROSS_ACCOUNT_ACCOUNT_ID:',
        ]

        # Map ALL_CAPS labels to Title Case display names
        label_display_map = {
            'IDENTIFIER:': 'Identifier:',
            'METHOD:': 'Method:',
            'ROUTE:': 'Route:',
            'CREATE_DLQ:': 'Create DLQ:',
            'FIFO_QUEUE:': 'FIFO Queue:',
            'VISIBILITY_TIMEOUT_SECONDS:': 'Visibility Timeout (seconds):',
            'MAX_RECEIVE_COUNT:': 'Max Receive Count:',
            'MESSAGE_RETENTION_SECONDS:': 'Message Retention (seconds):',
            'DLQ_MESSAGE_RETENTION_SECONDS:': 'DLQ Message Retention (seconds):',
            'CROSS_ACCOUNT_IDS:': 'Cross-Account IDs:',
            'PARTITION_KEY:': 'Partition Key:',
            'PARTITION_KEY_TYPE:': 'Partition Key Type:',
            'VERSIONING:': 'Versioning:',
            'ENABLE_S3_REPLICATION:': 'Enable S3 Replication:',
            'CROSS_ACCOUNT_ACCOUNT_ID:': 'Cross-Account Account ID:',
        }

        lines = response.split('\n')
        cleaned_lines = []

        for line in lines:
            line_stripped = line.strip()
            line_upper = line_stripped.upper()

            # Skip control flag lines
            if any(line_upper.startswith(flag.upper()) for flag in control_flags):
                continue

            # Transform parameter labels to Title Case
            transformed = False
            for label in param_labels:
                if line_upper.startswith(label):
                    # Extract the value part after the label
                    colon_idx = line_stripped.find(':')
                    if colon_idx != -1:
                        value = line_stripped[colon_idx + 1:].strip()
                        display_label = label_display_map.get(label, label.title())
                        cleaned_lines.append(f"{display_label} {value}")
                        transformed = True
                        break

            if not transformed:
                cleaned_lines.append(line)

        # Remove leading/trailing empty lines
        cleaned_text = '\n'.join(cleaned_lines).strip()

        # Build footer (use "general" when service_type is null)
        display_service = service_type if service_type else "general"
        status = "ready" if is_ready else "pending"
        if redirect:
            status = "redirect"
        footer = f"\n\n---\nservice: {display_service} | status: {status}"
        cleaned_text += footer

        return cleaned_text

    # ==================== Node 1: Load or Create Chat Session ====================
    async def load_or_create_chat_session(self, state: ChatState) -> ChatState:
        """
        Node 1: Find existing chat session or create new one based on context.
        Load conversation history (last 10 messages or all if < 10) and summary.
        """
        context = state["context"]

        # Find or create chat_info
        # Context: geo_loc + case_type + product + env + vendor + service + user + tenant
        chat_info, is_new = await self.chat_info_repo.find_or_create(
            tenants_mst_code=context.tenants_mst_code,
            user_mst_code=context.user_mst_code,
            infra_vendor_enum=context.infra_vendor_enum,
            applications_mst_code=context.applications_mst_code,
            resource_group_mst_code=context.resource_group_mst_code,
            services_mst_code=context.services_mst_code,
            environment_enum=context.environment_enum,
            geo_loc_mst_code=context.geo_loc_mst_code,
            case_type_ref_code=context.case_type_ref_code,
        )

        state["chat_info_code"] = chat_info.code
        state["is_new_session"] = is_new

        # Get case_code from state
        case_code = state.get("case_code", "general_chat")

        # Count total messages for this case
        total_count = await self.chat_message_repo.count_messages(
            chat_info_code=chat_info.code,
            include_summarized=True,
            case_code=case_code
        )
        state["total_message_count"] = total_count

        # Load conversation history filtered by case_code
        if total_count <= self.message_threshold:
            # Load all messages for this case
            messages = await self.chat_message_repo.get_messages_by_chat(
                chat_info_code=chat_info.code,
                include_summarized=True,
                case_code=case_code
            )
            state["conversation_history"] = [
                {"role": msg.role, "message": msg.message}
                for msg in messages
            ]
            state["summary"] = None
        else:
            # Load last N messages for this case
            messages = await self.chat_message_repo.get_messages_by_chat(
                chat_info_code=chat_info.code,
                limit=self.message_threshold,
                include_summarized=False,
                case_code=case_code
            )
            state["conversation_history"] = [
                {"role": msg.role, "message": msg.message}
                for msg in messages
            ]

            # Load summary for this case
            summary_obj = await self.chat_summary_repo.get_by_chat_and_case(
                chat_info.code,
                case_code
            )
            state["summary"] = summary_obj.summary_text if summary_obj else None

        return state

    # ==================== Node 2: Enrich Infrastructure Context ====================
    async def enrich_infrastructure_context(self, state: ChatState) -> ChatState:
        """
        Node 2: Fetch infrastructure metadata based on selected context.
        Build context string with relevant infrastructure details.
        """
        context = state["context"]
        context_parts = []

        # Build context information
        context_parts.append(f"Tenant: {context.tenants_mst_code}")
        context_parts.append(f"Infrastructure Vendor: {context.infra_vendor_enum.value}")

        if context.applications_mst_code:
            context_parts.append(f"Application: {context.applications_mst_code}")

        if context.resource_group_mst_code:
            context_parts.append(f"Resource Group: {context.resource_group_mst_code}")

        if context.services_mst_code:
            context_parts.append(f"Service: {context.services_mst_code}")

        if context.environment_enum:
            context_parts.append(f"Environment: {context.environment_enum.value}")

        # TODO: In future, fetch additional metadata from database
        # - Application details, resource counts
        # - Service configurations
        # - Recent monitoring alerts
        # - Resource group resources

        state["infrastructure_context"] = "\n".join(context_parts)
        return state

    # ==================== Node 3: Generate Response ====================
    async def generate_response(self, state: ChatState) -> ChatState:
        """
        Node 3: Extract AWS resource configuration and render Terragrunt template if ready.

        This node supports multiple services with dynamic detection:
        - S3 buckets: Extracts bucket identifier
        - SQS queues: Extracts queue identifier, create_dlq, and fifo_queue flags
        - Kong Gateway routes: Extracts service_name, method, and route pattern

        The LLM automatically detects which service the user wants based on their message.
        Users can switch between S3, SQS, and Gateway within the same conversation.
        """
        try:
            # Use unified AWS resource extraction (supports S3 and SQS)
            extraction_result = await self.openai_service.extract_aws_resource_config(
                conversation_history=state["conversation_history"],
                user_message=state["user_message"],
                infrastructure_context=state["infrastructure_context"],
                case_type_code=state["case_type_code"],
                case_code=state["case_code"],
                summary=state.get("summary"),
                session=self.session
            )

            # Store raw response for DB context (includes control flags for LLM history)
            state["ai_response_raw"] = extraction_result["response"]

            # Store service_type, is_ready, and redirect in state for frontend
            state["service_type"] = extraction_result.get("service_type")
            state["is_ready"] = extraction_result.get("ready", False)
            state["redirect"] = extraction_result.get("redirect", False)

            # Format display response (strip control flags, add footer)
            state["ai_response"] = self._format_display_response(
                response=extraction_result["response"],
                service_type=state["service_type"],
                is_ready=state["is_ready"],
                redirect=state["redirect"]
            )

            # Add validation warnings to display (only affects display, not DB-stored message)
            validation_warnings = extraction_result.get("validation_warnings", [])
            if validation_warnings and state["is_ready"]:
                warnings_text = "\n\n⚠️ Omitted invalid parameters:\n" + "\n".join(f"  • {w}" for w in validation_warnings)
                state["ai_response"] = state["ai_response"].replace(
                    "\n\n---\n",
                    f"{warnings_text}\n\n---\n"
                )

            # General validation: Check if selected service exists in database
            services_mst_code = state["context"].services_mst_code

            if services_mst_code:
                # Query database to verify service exists
                services_repo = ServicesMstRepository(self.session)
                service = await services_repo.get_by_code(services_mst_code)

                if not service:
                    # Service not found - override response
                    error_message = (
                        f"The selected service (code: '{services_mst_code}') was not found in the system. "
                        "Please select a valid service from the dropdown."
                    )
                    state["ai_response"] = error_message
                    state["is_ready"] = False
                    state["terraform_code"] = None
                    state["service_type"] = None
                    # Log and return early
                    await self._log_to_langfuse(state)
                    return state
                else:
                    # Cache service data in state for reuse (extract only needed fields for serialization)
                    state["selected_service"] = {
                        "name": service.name,
                        "service_type": service.service_type.value,  # Store enum value as string
                        "is_public_facing": service.is_public_facing
                    }

            # Validate services_mst_code for Gateway service type
            if extraction_result.get("service_type") == "gateway":
                services_mst_code = state["context"].services_mst_code

                if not services_mst_code:
                    # Override response - user must select service from dropdown
                    state["ai_response"] = (
                        "SERVICE_TYPE: gateway\n"
                        "READY: false\n\n"
                        "Please select a service from the dropdown before adding a gateway route.\n\n"
                        "To add a Kong Gateway route, you need to:\n"
                        "1. Select the API service from the 'Service' dropdown in the UI\n"
                        "2. Then provide the HTTP method and route pattern in the chat"
                    )
                    state["is_ready"] = False
                    state["terraform_code"] = None
                    # Log and return early - skip route addition
                    await self._log_to_langfuse(state)
                    return state
                else:
                    # Validate service is of type API (not BACKGROUND_SERVICE)
                    service = state.get("selected_service")

                    if service and service["service_type"] != ServiceTypeEnum.API.value:
                        # Override response - service is not an API
                        state["ai_response"] = (
                            "SERVICE_TYPE: gateway\n"
                            "READY: false\n\n"
                            f"Cannot add gateway route to '{service['name']}' because it is a "
                            f"{service['service_type']} service, not an API service.\n\n"
                            "Please select an API service from the dropdown to add gateway routes."
                        )
                        state["is_ready"] = False
                        state["terraform_code"] = None
                        await self._log_to_langfuse(state)
                        return state

                    # Validate service is public facing
                    if service and not service.get("is_public_facing", False):
                        # Override response - service is not public facing
                        state["ai_response"] = (
                            "SERVICE_TYPE: gateway\n"
                            "READY: false\n\n"
                            f"Cannot add gateway route to '{service['name']}' because it is not "
                            f"a public-facing service.\n\n"
                            "Gateway routes can only be added to services that are publicly accessible. "
                            "Please select a public-facing API service from the dropdown."
                        )
                        state["is_ready"] = False
                        state["terraform_code"] = None
                        await self._log_to_langfuse(state)
                        return state

            # If configuration is ready, render appropriate Terragrunt template or add gateway route
            if extraction_result["ready"]:
                template_service = TemplateService()
                service_type = extraction_result.get("service_type")

                if service_type == "s3" and extraction_result.get("identifier"):
                    # Render S3 bucket configuration
                    try:
                        terragrunt_config = template_service.render_s3_terragrunt(
                            identifier=extraction_result["identifier"],
                            versioning=extraction_result.get("versioning", False),
                            enable_s3_replication=extraction_result.get("enable_s3_replication", False),
                            cross_account_account_id=extraction_result.get("cross_account_account_id")
                        )
                        state["terraform_code"] = terragrunt_config
                    except ValueError as e:
                        # Validation error - communicate this back to the user via chat
                        error_message = str(e)
                        state["ai_response"] = error_message
                        state["terraform_code"] = None
                        state["is_ready"] = False
                    except Exception as e:
                        # Other errors (runtime, file not found, etc.)
                        error_message = f"Error creating S3 configuration:\n\n{str(e)}"
                        state["ai_response"] = error_message
                        state["terraform_code"] = None
                        state["is_ready"] = False

                elif service_type == "sqs" and extraction_result.get("identifier"):
                    # Render SQS queue configuration
                    try:
                        terragrunt_config = template_service.render_sqs_terragrunt(
                            identifier=extraction_result["identifier"],
                            create_dlq=extraction_result.get("create_dlq", True),
                            fifo_queue=extraction_result.get("fifo_queue", True),
                            visibility_timeout_seconds=extraction_result.get("visibility_timeout_seconds"),
                            max_receive_count=extraction_result.get("max_receive_count"),
                            message_retention_seconds=extraction_result.get("message_retention_seconds"),
                            dlq_message_retention_seconds=extraction_result.get("dlq_message_retention_seconds"),
                            cross_account_ids=extraction_result.get("cross_account_ids")
                        )
                        state["terraform_code"] = terragrunt_config
                    except ValueError as e:
                        # Validation error - communicate this back to the user via chat
                        error_message = str(e)
                        state["ai_response"] = error_message
                        state["terraform_code"] = None
                        state["is_ready"] = False
                    except Exception as e:
                        # Other errors (runtime, file not found, etc.)
                        error_message = f"Error creating SQS configuration:\n\n{str(e)}"
                        state["ai_response"] = error_message
                        state["terraform_code"] = None
                        state["is_ready"] = False

                elif service_type == "gateway" and extraction_result.get("method") and extraction_result.get("route"):
                    # Add route to Kong Gateway configuration
                    # Use cached service (already validated to exist and be of type API)
                    try:
                        service = state.get("selected_service")

                        if not service:
                            # This shouldn't happen due to earlier validations, but safety check
                            error_message = "Internal error: Service not found in state."
                            state["ai_response"] = error_message
                            state["terraform_code"] = None
                            state["is_ready"] = False
                        else:
                            # Use service name as api_name for Kong Gateway
                            # Normalize api_name: append -service if not already present
                            display_api_name = service["name"]
                            if display_api_name and not display_api_name.endswith('-service'):
                                display_api_name = f"{display_api_name}-service"
                            result_message = template_service.add_gateway_route(
                                api_name=display_api_name,
                                method=extraction_result["method"],
                                route=extraction_result["route"]
                            )
                            # For gateway, we don't return terraform_code, but we store the result
                            state["terraform_code"] = result_message
                    except ValueError as e:
                        # Validation error - communicate this back to the user via chat
                        error_message = str(e)
                        state["ai_response"] = error_message
                        state["terraform_code"] = None
                        state["is_ready"] = False
                    except Exception as e:
                        # Other errors (runtime, file not found, etc.)
                        error_message = f"Error adding route:\n\n{str(e)}"
                        state["ai_response"] = error_message
                        state["terraform_code"] = None
                        state["is_ready"] = False

                elif service_type == "dynamodb" and extraction_result.get("identifier") and extraction_result.get("partition_key"):
                    # Render DynamoDB table configuration
                    try:
                        terragrunt_config = template_service.render_dynamodb_terragrunt(
                            identifier=extraction_result["identifier"],
                            partition_key=extraction_result["partition_key"],
                            partition_key_type=extraction_result.get("partition_key_type", "S")
                        )
                        state["terraform_code"] = terragrunt_config
                    except ValueError as e:
                        # Validation error - communicate this back to the user via chat
                        error_message = str(e)
                        state["ai_response"] = error_message
                        state["terraform_code"] = None
                        state["is_ready"] = False
                    except Exception as e:
                        # Other errors (runtime, file not found, etc.)
                        error_message = f"Error creating DynamoDB configuration:\n\n{str(e)}"
                        state["ai_response"] = error_message
                        state["terraform_code"] = None
                        state["is_ready"] = False

                else:
                    # Unknown service type or missing parameters
                    state["terraform_code"] = None

                # Build parameters dict based on service_type (for frontend consumption)
                service_type = extraction_result.get("service_type")

                if service_type == "s3" and extraction_result.get("identifier"):
                    params = {
                        "identifier": extraction_result["identifier"],
                        "versioning": extraction_result.get("versioning", False),
                        "enable_s3_replication": extraction_result.get("enable_s3_replication", False)
                    }
                    # Only add cross_account_account_id if replication is enabled
                    if extraction_result.get("cross_account_account_id"):
                        params["cross_account_account_id"] = extraction_result["cross_account_account_id"]
                    state["parameters"] = params
                elif service_type == "sqs" and extraction_result.get("identifier"):
                    params = {
                        "identifier": extraction_result["identifier"],
                        "create_dlq": extraction_result.get("create_dlq", True),
                        "fifo_queue": extraction_result.get("fifo_queue", True)
                    }
                    # Only add optional params if provided
                    if extraction_result.get("visibility_timeout_seconds") is not None:
                        params["visibility_timeout_seconds"] = extraction_result["visibility_timeout_seconds"]
                    if extraction_result.get("max_receive_count") is not None:
                        params["max_receive_count"] = extraction_result["max_receive_count"]
                    if extraction_result.get("message_retention_seconds") is not None:
                        params["message_retention_seconds"] = extraction_result["message_retention_seconds"]
                    if extraction_result.get("dlq_message_retention_seconds") is not None:
                        params["dlq_message_retention_seconds"] = extraction_result["dlq_message_retention_seconds"]
                    if extraction_result.get("cross_account_ids"):
                        params["cross_account_ids"] = extraction_result["cross_account_ids"]

                    state["parameters"] = params
                elif service_type == "gateway" and extraction_result.get("method") and extraction_result.get("route"):
                    service = state.get("selected_service")
                    state["parameters"] = {
                        "api_name": service["name"] if service else None,
                        "method": extraction_result["method"],
                        "route": extraction_result["route"]
                    }
                elif service_type == "dynamodb" and extraction_result.get("identifier") and extraction_result.get("partition_key"):
                    state["parameters"] = {
                        "identifier": extraction_result["identifier"],
                        "partition_key": extraction_result["partition_key"],
                        "partition_key_type": extraction_result.get("partition_key_type", "S")
                    }
                else:
                    state["parameters"] = None

            else:
                # Not ready - no terraform code or parameters yet
                state["terraform_code"] = None
                state["parameters"] = None

            # Log to LangFuse
            await self._log_to_langfuse(state)

        except Exception as e:
            state["error"] = f"Error generating response: {str(e)}"
            state["ai_response"] = "I apologize, but I encountered an error processing your request. Please try again."

            # Log error to LangFuse
            await self._log_error_to_langfuse(e, state)

        return state

    # ==================== Node 4: Save Conversation ====================
    async def save_conversation(self, state: ChatState) -> ChatState:
        """
        Node 4: Save user message and AI response to database.
        Check if summarization is needed (total messages > threshold).
        If yes, summarize old messages and mark them as summarized.
        """
        try:
            chat_info_code = state["chat_info_code"]

            # Save user message
            await self.chat_message_repo.create_message(
                chat_info_code=chat_info_code,
                role="user",
                message=state["user_message"],
                case_code=state["case_code"]
            )

            # Save agent response (use raw response with control flags for LLM context)
            ai_response_for_db = state.get("ai_response_raw") or state.get("ai_response")
            await self.chat_message_repo.create_message(
                chat_info_code=chat_info_code,
                role="agent",
                message=ai_response_for_db,
                case_code=state["case_code"]
            )

            # Check if summarization is needed
            # After saving 2 new messages, total count = old count + 2
            new_total = state["total_message_count"] + 2

            if new_total > self.message_threshold:
                # Get unsummarized messages for this case (excluding last 10)
                messages_to_summarize = await self.chat_message_repo.get_unsummarized_messages(
                    chat_info_code=chat_info_code,
                    exclude_last_n=self.message_threshold,
                    case_code=state["case_code"]
                )

                if messages_to_summarize:
                    # Generate summary
                    messages_data = [
                        {"role": msg.role, "message": msg.message}
                        for msg in messages_to_summarize
                    ]

                    summary_text = await self.openai_service.generate_summary(messages_data)

                    # Save or update summary for this case
                    await self.chat_summary_repo.create_or_update_summary(
                        chat_info_code=chat_info_code,
                        case_code=state["case_code"],
                        summary_text=summary_text,
                        message_count=len(messages_to_summarize)
                    )

                    # Mark messages as summarized
                    message_ids = [msg.id for msg in messages_to_summarize]
                    await self.chat_message_repo.mark_as_summarized(message_ids)

            # Commit transaction
            await self.session.commit()

        except Exception as e:
            await self.session.rollback()
            state["error"] = f"Error saving conversation: {str(e)}"

        return state

    # ==================== Build Workflow ====================
    def _build_workflow(self) -> StateGraph:
        """Build LangGraph workflow with nodes and edges"""
        workflow = StateGraph(ChatState)

        # Add nodes
        workflow.add_node("load_or_create_chat_session", self.load_or_create_chat_session)
        workflow.add_node("enrich_infrastructure_context", self.enrich_infrastructure_context)
        workflow.add_node("generate_response", self.generate_response)
        workflow.add_node("save_conversation", self.save_conversation)

        # Define edges (linear flow)
        workflow.set_entry_point("load_or_create_chat_session")
        workflow.add_edge("load_or_create_chat_session", "enrich_infrastructure_context")
        workflow.add_edge("enrich_infrastructure_context", "generate_response")
        workflow.add_edge("generate_response", "save_conversation")
        workflow.add_edge("save_conversation", END)

        return workflow

    # ==================== LangFuse Logging ====================
    async def _log_to_langfuse(self, state: ChatState):
        """Log successful chat interaction to LangFuse"""
        try:
            # Convert conversation history to LangChain message objects
            conversation_messages = []

            # Add system prompt (use unified AWS resource prompt for S3 and SQS)
            system_prompt = self.openai_service._build_aws_resource_prompt()
            if state["infrastructure_context"]:
                system_prompt += f"\n\nInfrastructure Context:\n{state['infrastructure_context']}"
            conversation_messages.append(SystemMessage(content=system_prompt))

            # Add conversation summary if exists
            if state.get("summary"):
                conversation_messages.append(
                    SystemMessage(content=f"Summary of earlier conversation:\n{state['summary']}")
                )

            # Convert conversation history from dict format to LangChain message objects
            for msg in state.get("conversation_history", []):
                if msg["role"] == "user":
                    conversation_messages.append(HumanMessage(content=msg["message"]))
                elif msg["role"] == "agent":
                    conversation_messages.append(AIMessage(content=msg["message"]))

            # Add current user message
            conversation_messages.append(HumanMessage(content=state["user_message"]))

            metadata = {
                "user_mst_code": state["context"].user_mst_code,
                "tenants_mst_code": state["context"].tenants_mst_code,
                "chat_info_code": state["chat_info_code"],
                "infra_vendor_enum": state["context"].infra_vendor_enum.value,
                "environment_enum": state["context"].environment_enum.value if state["context"].environment_enum else None,
                "applications_mst_code": state["context"].applications_mst_code,
                "resource_group_mst_code": state["context"].resource_group_mst_code,
                "services_mst_code": state["context"].services_mst_code,
                "infrastructure_context": state["infrastructure_context"],
                "terraform_code": state.get("terraform_code"),
                "model_used": settings.openai_model,
                "conversation_messages": conversation_messages,  # Pass LangChain message objects
            }

            await langfuse_service.log_chat_interaction(
                user_query=state["user_message"],
                ai_response=state["ai_response"],
                metadata=metadata
            )

        except Exception as e:
            logger.error(f"Failed to log to LangFuse: {str(e)}")
            # Don't re-raise - logging failure shouldn't break chat

    async def _log_error_to_langfuse(self, error: Exception, state: ChatState):
        """Log errors to LangFuse for monitoring"""
        try:
            context = {
                "user_mst_code": state["context"].user_mst_code,
                "tenants_mst_code": state["context"].tenants_mst_code,
                "chat_info_code": state.get("chat_info_code", "unknown"),
                "user_message": state.get("user_message", ""),
                "infra_vendor_enum": state["context"].infra_vendor_enum.value,
            }

            await langfuse_service.log_error(error, context)

        except Exception as e:
            logger.error(f"Failed to log error to LangFuse: {str(e)}")
            # Don't re-raise - logging failure shouldn't break chat

    # ==================== Execute Chat ====================
    async def execute_chat(
        self,
        user_message: str,
        context: ChatContextSchema,
        case_code: str,
        case_type_code: str
    ) -> Dict:
        """
        Execute chat workflow.

        Args:
            user_message: User's message
            context: Infrastructure context from UI dropdowns
            case_code: Case code from case_ref table
            case_type_code: Case type code from case_type_ref table (for future routing)

        Returns:
            Dict with response data
        """
        # Compile workflow
        memory = MemorySaver()
        app = self.workflow.compile(checkpointer=memory)

        # Initial state
        initial_state: ChatState = {
            "user_message": user_message,
            "context": context,
            "case_code": case_code,
            "case_type_code": case_type_code,
            "chat_info_code": "",
            "is_new_session": False,
            "conversation_history": [],
            "summary": None,
            "total_message_count": 0,
            "infrastructure_context": "",
            "selected_service": None,
            "ai_response": "",
            "terraform_code": None,
            "service_type": None,
            "is_ready": False,
            "redirect": False,
            "parameters": None,
            "error": None,
        }

        # Execute workflow
        config = {"configurable": {"thread_id": "infrastructure_studio_chat"}}
        final_state = await app.ainvoke(initial_state, config)

        # Return response
        return {
            "chat_info_code": final_state["chat_info_code"],
            "response": final_state["ai_response"],
            "terraform_code": final_state.get("terraform_code"),
            "service_type": final_state.get("service_type"),
            "is_ready": final_state.get("is_ready", False),
            "redirect": final_state.get("redirect", False),
            "parameters": final_state.get("parameters"),
            "is_new_session": final_state["is_new_session"],
            "error": final_state.get("error"),
        }

    async def get_chat_by_context(
        self,
        context: ChatContextSchema
    ) -> Dict[str, any]:
        """
        Fetch all chat messages for a specific infrastructure context combination.

        This method finds existing chat sessions based on exact context match
        and returns all messages without creating a new session.

        Uses ChatInfoRepository to generate the deterministic code (DRY principle).

        Args:
            context: Infrastructure context with tenant, user, vendor, and optional fields

        Returns:
            Dict containing:
            - chat_info_code: The chat session code (None if doesn't exist)
            - chat_exists: Boolean flag indicating if chat exists
            - total_messages: Count of messages
            - messages: List of message dictionaries
            - context: The context used for filtering

        Raises:
            HTTPException: If database error occurs
        """
        import logging
        from fastapi import HTTPException

        logger = logging.getLogger(__name__)

        try:
            # Step 1: Generate chat_info_code using ChatInfoRepository logic
            # Context: geo_loc + case_type + product + env + vendor + service + user + tenant
            chat_info_repo = ChatInfoRepository(self.session)
            chat_info_code = chat_info_repo._generate_chat_code(
                tenants_mst_code=context.tenants_mst_code,
                user_mst_code=context.user_mst_code,
                infra_vendor_enum=context.infra_vendor_enum,
                applications_mst_code=context.applications_mst_code,
                resource_group_mst_code=context.resource_group_mst_code,
                services_mst_code=context.services_mst_code,
                environment_enum=context.environment_enum,
                geo_loc_mst_code=context.geo_loc_mst_code,
                case_type_ref_code=context.case_type_ref_code,
            )

            # Step 2: Check if chat_info exists
            chat_info = await chat_info_repo.get_by_code(chat_info_code)

            if not chat_info:
                # No chat exists for this context
                return {
                    "chat_info_code": None,
                    "chat_exists": False,
                    "total_messages": 0,
                    "messages": [],
                    "context": context.model_dump()
                }

            # Step 3: Fetch messages directly using existing method
            chat_msg_repo = ChatMessageRepository(self.session)
            messages = await chat_msg_repo.get_messages_by_chat(
                chat_info_code=chat_info_code,
                include_summarized=True
            )

            # Step 4: Transform messages to dict format
            messages_data = [
                {
                    "id": msg.id,
                    "code": msg.code,
                    "role": msg.role,
                    "message": msg.message,
                    "summary_status": msg.summary_status,
                    "created_at": msg.created_at,
                }
                for msg in messages
            ]

            return {
                "chat_info_code": chat_info_code,
                "chat_exists": True,
                "total_messages": len(messages),
                "messages": messages_data,
                "context": context.model_dump()
            }

        except Exception as e:
            logger.error(f"Error fetching chat by context: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch chat messages: {str(e)}"
            )

"""
Service Config Chat Service

Thin orchestrator for service config assistance chat.
Delegates core logic to ServiceConfigAgent (LangGraph workflow).
"""

import asyncio
import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.langfuse_service import langfuse_service
from app.integrations.openai_integration import OpenAIIntegration
from app.repository.chat_info_repository import ChatInfoRepository
from app.repository.chat_message_repository import ChatMessageRepository
from app.repository.service_config_chat_repository import ServiceConfigChatRepository
from app.utils.service_config_chat.prompt_builder import PromptBuilder
from app.utils.service_config_chat.intent_detector import IntentDetector
from app.utils.service_config_chat.legacy_intent_handler import LegacyIntentHandler
from app.utils.service_config_chat.service_matcher import ServiceMatcher
from app.utils.service_config_chat.parameter_helper import ParameterHelper
from app.services.service_config_agent import (
    ServiceConfigAgent,
    WorkflowFactory,
)
from app.schemas.service_config_chat_schemas import (
    ServiceConfigChatRequestSchema,
    ServiceConfigChatResponseSchema,
)


class ServiceConfigChatService:
    """
    Thin orchestrator for service config chat.

    Responsibilities (SRP):
    - Chat session lifecycle management
    - Message persistence
    - Observability logging
    - Delegates core logic to ServiceConfigAgent
    """

    def __init__(self, db: AsyncSession, use_agentic: bool = None):
        """
        Initialize service.

        Args:
            db: Database session
            use_agentic: Whether to use agentic workflow.
                        If None, uses settings.use_agentic_config_chat
        """
        self.db = db
        self.chat_repo = ChatInfoRepository(db)
        self.message_repo = ChatMessageRepository(db)
        self.config_repo = ServiceConfigChatRepository(db)
        self.prompts = PromptBuilder()

        # LLM client
        self.llm = OpenAIIntegration.get_chat_client(temperature=0.3)

        # Determine workflow type
        self._use_agentic = use_agentic if use_agentic is not None else getattr(
            settings, 'use_agentic_config_chat', False
        )

        # Create agent with injected dependencies
        self._agent = self._create_agent()

    def _create_agent(self) -> ServiceConfigAgent:
        """Create agent with dependency injection."""
        intent_detector = IntentDetector(self.llm)
        intent_handler = LegacyIntentHandler(self.db, self.llm)

        if self._use_agentic:
            return self._create_agentic_agent(intent_detector, intent_handler)
        else:
            return self._create_legacy_agent(intent_detector, intent_handler)

    def _create_legacy_agent(
        self,
        intent_detector: IntentDetector,
        intent_handler: LegacyIntentHandler,
    ) -> ServiceConfigAgent:
        """Create legacy workflow agent."""
        workflow = WorkflowFactory.create_legacy_workflow(
            intent_detector=intent_detector,
            intent_handler=intent_handler,
        )
        return ServiceConfigAgent(workflow)

    def _create_agentic_agent(
        self,
        intent_detector: IntentDetector,
        intent_handler: LegacyIntentHandler,
    ) -> ServiceConfigAgent:
        """Create agentic workflow agent with tools."""
        from app.utils.service_config_chat.route_classifier import RouteClassifier
        from app.utils.service_config_chat.query_planner import QueryPlanner
        from app.utils.service_config_chat.tool_executor import ToolExecutor
        from app.utils.service_config_chat.response_synthesizer import ResponseSynthesizer
        from app.services.service_config_agent.tools import (
            ServiceResolver,
            ParameterResolver,
            GetServiceConfig,
            GetParameterValue,
            SearchServicesByParameter,
            SemanticParameterSearch,
            SemanticConfigSearch,
            CompareConfigs,
            CompareEnvironments,
            GetDeploymentStatus,
            GetServiceDependencies,
        )
        from app.services.service_config_agent.tools.recommendation_tools import (
            GetRecommendations,
            ValidateConfig,
        )
        from app.services.service_config_agent.tools.listener_priority_tool import (
            GetAvailableListenerPriority,
        )
        from app.utils.service_config_chat.vector_parameter_matcher import VectorParameterMatcher
        from app.utils.service_config_chat.config_vectorizer import ConfigVectorizer

        # Create resolvers
        service_matcher = ServiceMatcher()
        parameter_helper = ParameterHelper()
        service_resolver = ServiceResolver(service_matcher)
        parameter_resolver = ParameterResolver(parameter_helper)

        # Create tools with injected dependencies
        vector_matcher = VectorParameterMatcher()
        config_vectorizer = ConfigVectorizer()
        tools = {
            "get_service_config": GetServiceConfig(
                service_resolver=service_resolver,
                repository=self.config_repo,
            ),
            "get_parameter_value": GetParameterValue(
                service_resolver=service_resolver,
                parameter_resolver=parameter_resolver,
                repository=self.config_repo,
            ),
            "search_services_by_parameter": SearchServicesByParameter(
                parameter_resolver=parameter_resolver,
                repository=self.config_repo,
            ),
            "semantic_parameter_search": SemanticParameterSearch(
                vector_matcher=vector_matcher,
            ),
            "semantic_config_search": SemanticConfigSearch(
                vectorizer=config_vectorizer,
            ),
            "compare_configs": CompareConfigs(
                service_resolver=service_resolver,
                repository=self.config_repo,
            ),
            "compare_environments": CompareEnvironments(
                service_resolver=service_resolver,
                repository=self.config_repo,
            ),
            "get_deployment_status": GetDeploymentStatus(
                service_resolver=service_resolver,
                repository=self.config_repo,
            ),
            "get_service_dependencies": GetServiceDependencies(
                service_resolver=service_resolver,
                repository=self.config_repo,
            ),
            "get_recommendations": GetRecommendations(
                repository=self.config_repo,
            ),
            "validate_config": ValidateConfig(
                repository=self.config_repo,
            ),
            "get_available_listener_priority": GetAvailableListenerPriority(
                repository=self.config_repo,
            ),
        }

        # Create services
        route_classifier = RouteClassifier(self.llm)
        query_planner = QueryPlanner(self.llm)
        tool_executor = ToolExecutor(tools)
        synthesizer = ResponseSynthesizer(self.llm)

        # Create workflow
        workflow = WorkflowFactory.create_agentic_workflow(
            route_classifier=route_classifier,
            intent_detector=intent_detector,
            intent_handler=intent_handler,
            query_planner=query_planner,
            tool_executor=tool_executor,
            synthesizer=synthesizer,
        )

        return ServiceConfigAgent(workflow)

    async def _resolve_reference_service_code(
        self,
        reference_service_name: Optional[str],
        tenants_mst_code: str,
    ) -> Optional[str]:
        """Resolve reference service name to code."""
        if not reference_service_name:
            return None
        services = await self.config_repo.get_services_by_tenant(tenants_mst_code)
        for s in services:
            if s.name.lower() == reference_service_name.lower():
                return s.code
        return None

    async def _check_reference_has_config(
        self,
        reference_service_code: Optional[str],
        context,
        tenants_mst_code: str,
    ) -> bool:
        """Check if reference service has config."""
        if not reference_service_code:
            return False
        return await self.config_repo.check_config_exists(
            service_code=reference_service_code,
            environment=context.environment_enum,
            geo_loc_code=context.geo_loc_mst_code,
            tenant_code=tenants_mst_code,
        )

    async def execute(
        self,
        request: ServiceConfigChatRequestSchema,
        tenants_mst_code: str,
        user_mst_code: str,
    ) -> ServiceConfigChatResponseSchema:
        """
        Execute chat request.

        Thin orchestrator that:
        1. Manages chat session
        2. Delegates to agent
        3. Persists messages
        4. Logs to Langfuse
        """
        start_time = time.perf_counter()
        context = request.context
        current_service_code = context.service_code

        # Resolve reference service
        reference_service_code = await self._resolve_reference_service_code(
            context.reference_service_name, tenants_mst_code
        )

        # Load or create chat session
        chat_info, is_new = await self.chat_repo.find_or_create_service_config_chat(
            tenants_mst_code=tenants_mst_code,
            user_mst_code=user_mst_code,
            geo_loc_mst_code=context.geo_loc_mst_code,
            environment_enum=context.environment_enum,
            services_mst_code=current_service_code,
        )

        # Load chat history
        history = await self.message_repo.get_messages_by_chat(
            chat_info_code=chat_info.code,
            limit=10,
            case_code="service_config_general",
        )

        # Handle new session welcome
        if is_new and not request.message.strip():
            return ServiceConfigChatResponseSchema(
                chat_info_code=chat_info.code,
                response=self.prompts.get_welcome_message(),
                intent="welcome",
                matched_services=None,
                config_json=None,
                is_ready=False,
            )

        # Check if reference has config
        reference_has_config = await self._check_reference_has_config(
            reference_service_code, context, tenants_mst_code
        )

        # Delegate to agent with proper error handling
        try:
            response_data = await self._agent.execute(
                user_message=request.message,
                context=context,
                tenants_mst_code=tenants_mst_code,
                user_mst_code=user_mst_code,
                chat_info_code=chat_info.code,
                is_new_session=is_new,
                history=history,
                reference_service_code=reference_service_code,
                reference_has_config=reference_has_config,
            )
        except Exception as e:
            # Rollback transaction on agent error to prevent cascade failures
            await self.db.rollback()
            response_data = {
                "response": f"I'm sorry, I encountered an error processing your request. Please try again.",
                "intent": "error",
                "error": str(e),
            }

        # Save messages (after potential rollback, transaction is clean)
        try:
            await self._save_messages(
                chat_info_code=chat_info.code,
                user_message=request.message,
                assistant_message=response_data["response"],
            )
        except Exception:
            # If saving fails, rollback and continue (don't fail the response)
            await self.db.rollback()

        # Log to Langfuse (fire-and-forget)
        latency_ms = (time.perf_counter() - start_time) * 1000
        asyncio.create_task(langfuse_service.log_service_sage_interaction(
            user_query=request.message,
            ai_response=response_data["response"],
            intent=response_data.get("intent", ""),
            chat_info_code=chat_info.code,
            user_id=user_mst_code,
            tenant_id=tenants_mst_code,
            metadata={
                "latency_ms": round(latency_ms, 2),
                "environment": context.environment_enum.value if context.environment_enum else None,
                "service_code": current_service_code,
                "workflow_type": "agentic" if self._use_agentic else "legacy",
            }
        ))

        return ServiceConfigChatResponseSchema(
            chat_info_code=chat_info.code,
            response=response_data["response"],
            intent=response_data.get("intent"),
            matched_services=response_data.get("matched_services"),
            reference_service_code=response_data.get("reference_service_code"),
            reference_service_name=response_data.get("reference_service_name"),
            config_json=response_data.get("config_json"),
            updation_field=response_data.get("updation_field"),
            is_ready=response_data.get("is_ready", False),
        )

    async def _save_messages(
        self,
        chat_info_code: str,
        user_message: str,
        assistant_message: str,
    ):
        """Save user and assistant messages."""
        await self.message_repo.create_message(
            chat_info_code=chat_info_code,
            role="user",
            message=user_message,
            case_code="service_config_general",
        )

        await self.message_repo.create_message(
            chat_info_code=chat_info_code,
            role="agent",
            message=assistant_message,
            case_code="service_config_general",
        )

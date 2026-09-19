"""
Legacy Intent Handler for Service Config Chat.

Single responsibility: handle all legacy intents.
Extracted from ServiceConfigChatService for SOLID compliance.
"""

import asyncio
import re
import time
from typing import Dict, Any, Optional, List

from sqlalchemy.ext.asyncio import AsyncSession
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from app.services.langfuse_service import langfuse_service
from app.repository.service_config_chat_repository import ServiceConfigChatRepository
from app.utils.service_config_chat.service_matcher import ServiceMatcher
from app.utils.service_config_chat.config_builder import ConfigBuilder
from app.utils.service_config_chat.prompt_builder import PromptBuilder
from app.utils.service_config_chat.parameter_helper import ParameterHelper
from app.utils.service_config_chat.vector_parameter_matcher import MatchConfidence
from app.utils.service_config_chat.prompts.parameter_definitions import is_autofill_allowed
from app.utils.service_config_chat.prompts.intent_prompt import CUSTOM_VALUE_EXTRACTION_PROMPT
from app.utils.service_config_chat.value_extractor import extract_value_regex, is_confirmation_only
from app.schemas.service_config_chat_schemas import ServiceMatchSchema


class LegacyIntentHandler:
    """
    Handles all legacy intents for service config chat.

    Responsibilities:
    - Dispatch to appropriate handler based on intent
    - Execute intent-specific logic
    - Build response data
    """

    def __init__(self, db: AsyncSession, llm):
        """
        Initialize handler with dependencies.

        Args:
            db: Database session
            llm: LangChain chat client
        """
        self.db = db
        self.llm = llm
        self.config_repo = ServiceConfigChatRepository(db)
        self.matcher = ServiceMatcher()
        self.builder = ConfigBuilder()
        self.prompts = PromptBuilder()
        self.param_helper = ParameterHelper()

    async def handle(
        self,
        intent: str,
        message: str,
        context,
        tenants_mst_code: str,
        reference_service_code: Optional[str],
        history: List = None,
    ) -> Dict[str, Any]:
        """
        Dispatch to appropriate handler based on intent.

        Args:
            intent: Detected intent
            message: User message
            context: Service config context
            tenants_mst_code: Tenant code
            reference_service_code: Reference service code if selected
            history: Conversation history

        Returns:
            Response data dict
        """
        handlers = {
            self.prompts.INTENT_LIST_SERVICES: self._handle_list_services,
            self.prompts.INTENT_SELECT_SERVICE: self._handle_select_service,
            self.prompts.INTENT_SHOW_CONFIG: self._handle_show_config,
            self.prompts.INTENT_FORM_FILL: self._handle_form_fill,
            self.prompts.INTENT_DECLINE: self._handle_decline,
            self.prompts.INTENT_ASK_PARAMETER: self._handle_ask_parameter,
            self.prompts.INTENT_LIST_PARAMETERS: self._handle_list_parameters,
            self.prompts.INTENT_HELP: self._handle_help,
            self.prompts.INTENT_OUT_OF_SCOPE: self._handle_out_of_scope,
        }

        handler = handlers.get(intent, self._handle_out_of_scope)

        # Build handler kwargs based on what each handler needs
        kwargs = {
            "message": message,
            "context": context,
            "tenants_mst_code": tenants_mst_code,
            "reference_service_code": reference_service_code,
            "history": history,
        }

        return await handler(**kwargs)

    # ==================== Intent Handlers ====================

    async def _handle_list_services(
        self,
        tenants_mst_code: str,
        context,
        history: List = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Handle list_services intent."""
        services_with_status = await self.config_repo.get_services_with_any_config(
            tenant_code=tenants_mst_code,
            environment=context.environment_enum,
            infra_vendor=context.infra_vendor,
            infrastructure_type=context.infrastructure_type,
        )

        current_service_code = context.service_code
        services_data = [
            s for s in services_with_status
            if s["service_code"] != current_service_code and s["has_existing_config"]
        ]

        # Build infrastructure context
        infra_parts = []
        if context.infra_vendor:
            infra_parts.append(context.infra_vendor.upper())
        if context.infrastructure_type:
            infra_parts.append(context.infrastructure_type)
        infra_context = f"Infrastructure: {' '.join(infra_parts)}" if infra_parts else ""
        infra_suffix = f" {' '.join(infra_parts)}" if infra_parts else ""

        prompt_context = {
            "services": services_data,
            "infra_context": infra_context,
            "infra_suffix": infra_suffix,
        }
        response = await self._generate_response(
            self.prompts.INTENT_LIST_SERVICES,
            prompt_context,
            history,
        )

        matched_services = [
            ServiceMatchSchema(
                service_code=s["service_code"],
                service_name=s["service_name"],
                service_type=s["service_type"],
                similarity_score=1.0,
                has_existing_config=True,
            )
            for s in services_data
        ]

        return {
            "response": response,
            "matched_services": matched_services,
        }

    async def _handle_select_service(
        self,
        message: str,
        tenants_mst_code: str,
        context,
        history: List = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Handle select_service intent."""
        services_with_status = await self.config_repo.get_services_with_any_config(
            tenant_code=tenants_mst_code,
            environment=context.environment_enum,
            infra_vendor=context.infra_vendor,
            infrastructure_type=context.infrastructure_type,
        )

        services_with_configs = [
            s for s in services_with_status
            if s["service_code"] != context.service_code and s["has_existing_config"]
        ]

        matches = self.matcher.match_from_dicts(message, services_with_configs)

        if not matches:
            prompt_context = {"matches": []}
            response = await self._generate_response(
                self.prompts.INTENT_SELECT_SERVICE,
                prompt_context,
                history,
            )
            return {"response": response}

        top_match = matches[0]

        # Check for ambiguous matches - multiple services with similar scores
        AMBIGUITY_DELTA = 0.1
        close_matches = [
            m for m in matches
            if top_match.similarity_score - m.similarity_score <= AMBIGUITY_DELTA
        ]

        if top_match.similarity_score >= 0.8 and len(close_matches) == 1:
            # Single clear match - show config directly
            result = await self._handle_show_config(
                context=context,
                reference_service_code=top_match.service_code,
                tenants_mst_code=tenants_mst_code,
                reference_service_name=top_match.service_name,
                history=history,
            )
            result["reference_service_code"] = top_match.service_code
            result["reference_service_name"] = top_match.service_name
            return result

        # Multiple close matches or low confidence - show selection prompt
        # Use close_matches if high confidence but ambiguous, otherwise all matches
        matches_to_show = close_matches if top_match.similarity_score >= 0.8 else matches

        prompt_context = {
            "matches": [
                {
                    "service_name": m.service_name,
                    "service_code": m.service_code,
                    "similarity_score": m.similarity_score,
                }
                for m in matches_to_show
            ]
        }
        response = await self._generate_response(
            self.prompts.INTENT_SELECT_SERVICE,
            prompt_context,
            history,
        )

        matched_services = [
            ServiceMatchSchema(
                service_code=m.service_code,
                service_name=m.service_name,
                service_type=m.service_type,
                similarity_score=m.similarity_score,
                has_existing_config=True,
            )
            for m in matches_to_show
        ]

        return {
            "response": response,
            "matched_services": matched_services,
        }

    async def _handle_show_config(
        self,
        context,
        reference_service_code: Optional[str],
        tenants_mst_code: str,
        reference_service_name: Optional[str] = None,
        history: List = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Handle show_config intent."""
        service_name = reference_service_name or context.reference_service_name
        if not service_name:
            return {
                "response": "Please select a reference service first. You can list services or type a service name to find one."
            }

        config, actual_geo_loc = await self.config_repo.get_config_with_fallback(
            service_code=reference_service_code,
            environment=context.environment_enum,
            preferred_geo_loc=context.geo_loc_mst_code,
            tenant_code=tenants_mst_code,
            infra_vendor=context.infra_vendor,
            infrastructure_type=context.infrastructure_type,
            infrastructure_mst_code=context.infrastructure_mst_code,
        )

        if not config:
            prompt_context = {
                "config": None,
                "reference_service_name": service_name,
            }
        else:
            config_dict = config.config or {}
            config_summary = self.builder.format_config_summary(config_dict)

            source_parts = []
            if config.infra_vendor_enum:
                source_parts.append(
                    config.infra_vendor_enum.value.upper()
                    if hasattr(config.infra_vendor_enum, 'value')
                    else str(config.infra_vendor_enum).upper()
                )
            if config.infrastructuretype_ref_code:
                source_parts.append(config.infrastructuretype_ref_code)
            if actual_geo_loc and actual_geo_loc != context.geo_loc_mst_code:
                source_parts.append(f"from {actual_geo_loc}")

            source_note = f"Source: {' '.join(source_parts)}" if source_parts else ""

            prompt_context = {
                "config": config_dict,
                "config_summary": config_summary,
                "reference_service_name": service_name,
                "source_note": source_note,
            }

        response = await self._generate_response(
            self.prompts.INTENT_SHOW_CONFIG,
            prompt_context,
            history,
        )

        return {"response": response}

    async def _handle_form_fill(
        self,
        message: str,
        context,
        reference_service_code: Optional[str],
        tenants_mst_code: str,
        history: List = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Handle form_fill intent."""
        pending_field = await self._extract_pending_field_fill(history, message)
        if pending_field:
            return {
                "response": f"Filling '{pending_field['field']}' with value '{pending_field['value']}'.",
                "updation_field": pending_field,
            }

        is_explicit_fill = "form" in message.lower() or message.lower().strip() == "autofill"

        if not context.reference_service_name or not reference_service_code or is_explicit_fill:
            return await self._handle_list_services(
                tenants_mst_code=tenants_mst_code,
                context=context,
                history=history,
            )

        is_simple_yes = message.lower().strip() in {"yes", "ok", "sure", "yep", "yeah"}
        if self._was_last_response_decline(history) and is_simple_yes:
            response = await self._generate_response(
                self.prompts.INTENT_CLARIFY_AFTER_DECLINE,
                {"reference_service_name": context.reference_service_name},
                history,
            )
            return {"response": response}

        config, _ = await self.config_repo.get_config_with_fallback(
            service_code=reference_service_code,
            environment=context.environment_enum,
            preferred_geo_loc=context.geo_loc_mst_code,
            tenant_code=tenants_mst_code,
            infra_vendor=context.infra_vendor,
            infrastructure_type=context.infrastructure_type,
            infrastructure_mst_code=context.infrastructure_mst_code,
        )

        if not config:
            return {"response": f"No config found for {context.reference_service_name}."}

        return {
            "response": f"Filling form with {context.reference_service_name} config.",
            "config_json": self.builder.build_response_config_json(config),
            "is_ready": True,
        }

    async def _handle_decline(self, history: List = None, **kwargs) -> Dict[str, Any]:
        """Handle decline intent."""
        response = await self._generate_response(
            self.prompts.INTENT_DECLINE,
            {},
            history,
        )
        return {"response": response}

    async def _handle_help(self, history: List = None, **kwargs) -> Dict[str, Any]:
        """Handle help intent."""
        response = await self._generate_response(
            self.prompts.INTENT_HELP,
            {},
            history,
        )
        return {"response": response}

    async def _handle_out_of_scope(
        self,
        message: str,
        history: List = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Handle out_of_scope intent."""
        prompt_context = {"user_message": message}
        response = await self._generate_response(
            self.prompts.INTENT_OUT_OF_SCOPE,
            prompt_context,
            history,
        )
        return {"response": response}

    async def _handle_list_parameters(self, history: List = None, **kwargs) -> Dict[str, Any]:
        """Handle list_parameters intent."""
        response = await self._generate_response(
            self.prompts.INTENT_LIST_PARAMETERS,
            {},
            history,
        )
        return {"response": response}

    async def _handle_ask_parameter(
        self,
        message: str,
        context,
        tenants_mst_code: str,
        history: List = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Handle ask_parameter intent."""
        # Check for disambiguation follow-up first
        param_name = self.param_helper.resolve_disambiguation(message, history)

        if not param_name:
            param_name = await self.param_helper.extract_parameter_name(self.llm, message)

        if not param_name:
            if self.param_helper.mentions_other_environment(message, context.environment_enum):
                current_env = context.environment_enum.value
                return {
                    "response": f"I can only show stats for {current_env}. Change the environment in config settings to see stats for other environments."
                }
            return {"response": "Which parameter would you like to know about?"}

        # Handle ambiguous parameters
        if param_name.upper() == "AMBIGUOUS_SCALING":
            return {"response": "Which scaling? Task autoscaling or HTTP request scaling?"}
        if param_name.upper() == "AMBIGUOUS_HPA":
            return {
                "response": "Which HPA parameter? replica_count, min_replicas, max_replicas, cpu_threshold, or memory_threshold?"
            }

        canonical, confidence, score = await self.param_helper.match_parameter_hybrid(param_name)

        if confidence == MatchConfidence.NO_MATCH or canonical is None:
            return {
                "response": f"I don't recognize '{param_name}' as a parameter. Say 'list parameters' to see what I can help with."
            }

        if confidence == MatchConfidence.UNCERTAIN:
            match = await self.param_helper.vector_matcher.match_parameter(param_name)
            alternatives = [canonical] + [a["canonical_name"] for a in match.alternatives[:2]]
            alt_list = ", ".join(alternatives)
            return {
                "response": f"Did you mean one of these: {alt_list}? Please clarify which parameter you're asking about."
            }

        definition = self.param_helper.get_definition(canonical)
        if not definition:
            return {
                "response": f"I don't recognize '{canonical}' as a parameter. Say 'list parameters' to see what I can help with."
            }

        stats = await self.config_repo.get_parameter_stats(
            tenant_code=tenants_mst_code,
            environment=context.environment_enum,
            parameter_name=canonical,
            infra_vendor=context.infra_vendor,
            infrastructure_type=context.infrastructure_type,
        )

        infra_parts = []
        if context.infra_vendor:
            infra_parts.append(context.infra_vendor.upper())
        if context.infrastructure_type:
            infra_parts.append(context.infrastructure_type)
        infra_context = f"Infrastructure: {' '.join(infra_parts)}" if infra_parts else ""
        infra_suffix = f" ({' '.join(infra_parts)})" if infra_parts else ""

        prompt_context = {
            "parameter_name": canonical,
            "definition": definition,
            "environment": context.environment_enum.value,
            "infra_context": infra_context,
            "infra_suffix": infra_suffix,
            "most_common": stats.get("most_common", "varies"),
            "total_services": stats.get("total_services", 0),
            "total_configs": stats.get("total_configs", 0),
            "outlier_note": self.param_helper.format_outliers(stats.get("outliers", [])),
        }

        response = await self._generate_response(
            self.prompts.INTENT_ASK_PARAMETER,
            prompt_context,
            history,
        )

        suggested_value = None
        most_common = stats.get("most_common")
        if most_common is not None and most_common != "varies" and stats.get("total_configs", 0) > 0:
            suggested_value = most_common

        if suggested_value is not None and is_autofill_allowed(canonical, context.infrastructure_type):
            response = response + f"\n\nWould you like me to fill '{canonical}' with value '{suggested_value}'?"

        return {"response": response}

    # ==================== Helper Methods ====================

    async def _generate_response(
        self,
        intent: str,
        context: Dict[str, Any],
        history: List = None,
    ) -> str:
        """Generate response using LLM."""
        prompt = self.prompts.build_response_prompt(intent, context)

        messages = [SystemMessage(content=self.prompts.get_system_prompt())]

        if history:
            for msg in history:
                role = msg.role if hasattr(msg, 'role') else msg.get('role', 'user')
                message = msg.message if hasattr(msg, 'message') else msg.get('message', '')
                if role == "user":
                    messages.append(HumanMessage(content=message))
                else:
                    messages.append(AIMessage(content=message))

        messages.append(HumanMessage(content=prompt))

        start_time = time.perf_counter()
        response = await self.llm.ainvoke(messages)
        result = response.content.strip()
        latency_ms = (time.perf_counter() - start_time) * 1000

        asyncio.create_task(langfuse_service.log_llm_call(
            call_type="response_generation",
            input_text=f"intent:{intent}",
            output_text=result[:200],
            latency_ms=latency_ms,
            metadata={"intent": intent}
        ))

        return result

    def _was_last_response_decline(self, history: List = None) -> bool:
        """Check if last assistant message was a decline acknowledgment."""
        if not history:
            return False
        for msg in reversed(history):
            role = msg.role if hasattr(msg, 'role') else msg.get('role', 'user')
            message = msg.message if hasattr(msg, 'message') else msg.get('message', '')
            if role == "agent":
                return "no problem" in message.lower() or "pick another" in message.lower()
        return False

    async def _extract_pending_field_fill(
        self,
        history: List,
        user_message: str,
    ) -> Optional[Dict[str, str]]:
        """Extract pending field fill with custom value support."""
        pending = self._get_pending_field_from_history(history)
        if not pending:
            return None

        custom_value = await self._extract_custom_value(
            user_message,
            pending['field'],
            pending['value']
        )

        if custom_value:
            pending['value'] = custom_value

        return pending

    def _get_pending_field_from_history(self, history: List) -> Optional[Dict[str, str]]:
        """Extract pending field fill from history."""
        if not history or len(history) < 1:
            return None

        for msg in reversed(history):
            role = msg.role if hasattr(msg, 'role') else msg.get('role', '')
            if role == "agent":
                message = msg.message if hasattr(msg, 'message') else msg.get('message', '')
                match = re.search(
                    r"Would you like me to fill '([^']+)' with value '([^']+)'\?",
                    message
                )
                if match:
                    return {"field": match.group(1), "value": match.group(2)}
                break

        return None

    async def _extract_custom_value(
        self,
        message: str,
        field: str,
        suggested_value: str,
    ) -> Optional[str]:
        """Extract custom value from user message."""
        if is_confirmation_only(message):
            return None

        regex_value = extract_value_regex(message)
        if regex_value:
            return regex_value

        prompt = CUSTOM_VALUE_EXTRACTION_PROMPT.format(
            field=field,
            suggested_value=suggested_value,
            user_message=message
        )

        messages = [HumanMessage(content=prompt)]
        response = await self.llm.ainvoke(messages)
        result = response.content.strip()

        if result.upper() == "NONE":
            return None
        return result

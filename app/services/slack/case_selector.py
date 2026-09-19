"""Hybrid Case Selection for Slack

Implements two-tier case selection:
1. Fast path: Auto-mapping for common services (S3, SQS, DynamoDB, Gateway)
2. LLM path: Intelligent search for ambiguous cases

This provides sub-100ms response for 90% of requests while handling edge cases gracefully.
"""
from typing import Optional, Dict, List, Any
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.case_ref_model import CaseRefModel
from app.db.models.case_type_ref_model import CaseTypeRefModel
from app.services.openai_service import OpenAIService
import logging

logger = logging.getLogger(__name__)


# Fast path: Service keyword → Case type mapping
# Maps to actual case_type_ref_code values in database
SERVICE_TO_CASE_TYPE = {
    "s3": "s3",
    "bucket": "s3",
    "storage": "s3",

    "sqs": "sqs",
    "queue": "sqs",

    "dynamodb": "dynamodb",
    "dynamo": "dynamodb",
    "table": "dynamodb",

    "gateway": "kong_gateway",
    "kong": "kong_gateway",
    "route": "kong_gateway",
    "api": "kong_gateway",

    "mysql": "mysql_database",
    "postgresql": "postgresql_database",
    "postgres": "postgresql_database",
    "database": "database",
}


class CaseSelector:
    """Selects appropriate case based on user message using hybrid approach."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.openai_service = OpenAIService()

    async def detect_case(
        self,
        user_message: str,
        tenant_code: str
    ) -> Dict[str, Any]:
        """Detect which case to use based on user message.

        Returns dict with:
        - case_code: Code of selected case (if auto-selected)
        - case_type_code: Code of case type
        - needs_selection: True if multiple cases found
        - options: List of case options (if needs_selection=True)
        - is_help_request: True if user is asking questions (not creating resources)

        Args:
            user_message: User's message
            tenant_code: Tenant code

        Returns:
            Detection result dictionary
        """
        # Step 1: LLM-based intent + service detection
        # This handles both help questions and creation requests intelligently
        logger.info("Using LLM for intent detection")
        llm_result = await self._extract_intent_and_service(user_message)
        logger.debug(f"[CASE DETECTION] LLM intent result: {llm_result}")

        # Check if LLM returned a valid result
        # Step 1.5: Keyword-based fallback for common services
        # Check keywords FIRST before trusting LLM intent classification
        # This handles cases like "hi i want to create s3" which LLM might classify as greeting
        message_lower = user_message.lower()

        if "s3" in message_lower or "bucket" in message_lower:
            logger.info(f"Keyword detected: S3 (LLM intent: {llm_result.get('intent') if llm_result else 'unknown'})")
            case_type_code = SERVICE_TO_CASE_TYPE.get("s3")
            result = await self._resolve_case_by_type(case_type_code, tenant_code)
            result["service_type"] = "s3"
            return result

        elif "sqs" in message_lower or "queue" in message_lower:
            logger.info(f"Keyword detected: SQS (LLM intent: {llm_result.get('intent') if llm_result else 'unknown'})")
            case_type_code = SERVICE_TO_CASE_TYPE.get("sqs")
            result = await self._resolve_case_by_type(case_type_code, tenant_code)
            result["service_type"] = "sqs"
            return result

        elif "dynamodb" in message_lower or "dynamo" in message_lower:
            logger.info(f"Keyword detected: DynamoDB (LLM intent: {llm_result.get('intent') if llm_result else 'unknown'})")
            case_type_code = SERVICE_TO_CASE_TYPE.get("dynamodb")
            result = await self._resolve_case_by_type(case_type_code, tenant_code)
            result["service_type"] = "dynamodb"
            return result

        elif "gateway" in message_lower or "kong" in message_lower:
            logger.info(f"Keyword detected: Gateway (LLM intent: {llm_result.get('intent') if llm_result else 'unknown'})")
            case_type_code = SERVICE_TO_CASE_TYPE.get("gateway")
            result = await self._resolve_case_by_type(case_type_code, tenant_code)
            result["service_type"] = "gateway"
            return result

        # No keywords found - now check LLM intent
        if llm_result:
            # If it's a greeting (and no service keywords found above), mark it as such
            if llm_result.get("intent") == "greeting":
                logger.info("Detected greeting (no service keywords)")
                return {
                    "case_code": "general_chat",
                    "case_type_code": "general",
                    "needs_selection": False,
                    "is_greeting": True
                }

            # If it's a help/question request, mark it as such
            if llm_result.get("intent") == "help":
                logger.info("Detected help/question request")
                return {
                    "case_code": "general_chat",
                    "case_type_code": "general",
                    "needs_selection": False,
                    "is_help_request": True
                }

            # If it's a creation request with identified service
            if llm_result.get("intent") == "create" and llm_result.get("service_type"):
                service_type = llm_result["service_type"]
                case_type_code = SERVICE_TO_CASE_TYPE.get(service_type.lower())

                if case_type_code:
                    logger.info(f"LLM matched service: {service_type} → {case_type_code}")
                    result = await self._resolve_case_by_type(case_type_code, tenant_code)
                    # Add service_type to the result so it's available downstream
                    result["service_type"] = service_type
                    return result

        # Step 2: Search cases by keywords (fallback)
        logger.info("LLM didn't identify specific service, searching by keywords")
        keywords = self._extract_keywords(user_message)
        cases = await self._search_cases_by_keywords(keywords, tenant_code)

        if len(cases) == 1:
            return {
                "case_code": cases[0]["code"],
                "case_type_code": cases[0]["case_type_code"],
                "needs_selection": False
            }
        elif len(cases) > 1:
            return {
                "needs_selection": True,
                "options": cases
            }

        # Step 3: No match - return general case
        logger.warning(f"No case matched for message: {user_message[:100]}")
        logger.warning(f"[CASE DETECTION] LLM result was: {llm_result}")
        return {
            "case_code": "general_chat",
            "case_type_code": "general",
            "needs_selection": False
        }

    async def _extract_intent_and_service(self, message: str) -> Optional[Dict[str, Any]]:
        """LLM-based intent and service type extraction.

        Args:
            message: User message

        Returns:
            Dict with intent and service_type
        """
        try:
            from openai import AsyncOpenAI
            from app.core.config import settings
            import json

            client = AsyncOpenAI(api_key=settings.openai_api_key)

            system_prompt = """You are an infrastructure assistant. Analyze the user's message and determine:
1. Intent: Is this a GREETING ONLY, request to CREATE infrastructure, or a HELP/question?
2. Service type: If creating, which service?

CRITICAL: If the message contains BOTH a greeting AND a creation request, classify as "create"

Examples:
- "Hi" → intent: greeting
- "Hello" → intent: greeting
- "Hey there" → intent: greeting
- "Hi, I want s3" → intent: create, service_type: s3
- "Hello, create an S3 bucket" → intent: create, service_type: s3
- "Hey, I need SQS" → intent: create, service_type: sqs
- "Create an S3 bucket" → intent: create, service_type: s3
- "What features do you have?" → intent: help
- "Do you support S3?" → intent: help
- "I need an SQS queue" → intent: create, service_type: sqs

Respond with ONLY a JSON object:
{"intent": "greeting|create|help", "service_type": "s3|sqs|dynamodb|gateway|unknown", "confidence": "high|medium|low"}"""

            response = await client.chat.completions.create(
                model=settings.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": message}
                ],
                temperature=0.3,
                max_tokens=50
            )

            # Parse JSON response
            content = response.choices[0].message.content.strip()

            # Try to extract JSON from response
            if "{" in content:
                json_start = content.index("{")
                json_end = content.rindex("}") + 1
                json_str = content[json_start:json_end]
                result = json.loads(json_str)

                if result.get("confidence") in ["high", "medium"]:
                    return result

            return None

        except Exception as e:
            logger.error(f"LLM extraction failed: {str(e)}")
            return None

    async def _resolve_case_by_type(
        self,
        case_type_code: str,
        tenant_code: str
    ) -> Dict[str, Any]:
        """Resolve case by case type.

        Args:
            case_type_code: Case type code
            tenant_code: Tenant code (not used - cases are global reference data)

        Returns:
            Resolution result
        """
        # Note: case_ref is global reference data, not tenant-specific
        stmt = select(CaseRefModel).where(
            CaseRefModel.case_type_ref_code == case_type_code,
            CaseRefModel.is_deleted == False
        )

        result = await self.db.execute(stmt)
        cases = result.scalars().all()

        if len(cases) == 1:
            # Auto-select
            return {
                "case_code": cases[0].code,
                "case_type_code": case_type_code,
                "needs_selection": False
            }
        elif len(cases) > 1:
            # Multiple cases - need user selection
            return {
                "needs_selection": True,
                "options": [
                    {
                        "code": case.code,
                        "name": case.name,
                        "case_type_code": case.case_type_ref_code
                    }
                    for case in cases
                ]
            }
        else:
            # No cases found
            return {
                "case_code": "general_chat",
                "case_type_code": "general",
                "needs_selection": False
            }

    def _extract_keywords(self, message: str) -> List[str]:
        """Extract keywords from message for case search.

        Args:
            message: User message

        Returns:
            List of keywords
        """
        # Simple keyword extraction (can be enhanced with NLP)
        words = message.lower().split()

        # Filter out common words
        stop_words = {"i", "need", "want", "create", "add", "the", "a", "an", "for", "to", "in", "on"}
        keywords = [w for w in words if w not in stop_words and len(w) > 2]

        return keywords[:5]  # Limit to top 5 keywords

    async def _search_cases_by_keywords(
        self,
        keywords: List[str],
        tenant_code: str
    ) -> List[Dict[str, Any]]:
        """Search cases by keywords in name/description.

        Args:
            keywords: List of search keywords
            tenant_code: Tenant code (not used - cases are global reference data)

        Returns:
            List of matching cases
        """
        if not keywords:
            return []

        # Build search query
        filters = []
        for keyword in keywords:
            filters.append(CaseRefModel.name.ilike(f"%{keyword}%"))
            filters.append(CaseRefModel.description.ilike(f"%{keyword}%"))

        # Note: case_ref is global reference data, not tenant-specific
        stmt = select(CaseRefModel).where(
            CaseRefModel.is_deleted == False,
            or_(*filters)
        ).limit(10)

        result = await self.db.execute(stmt)
        cases = result.scalars().all()

        return [
            {
                "code": case.code,
                "name": case.name,
                "case_type_code": case.case_type_ref_code
            }
            for case in cases
        ]

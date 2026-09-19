"""Slack Validation Service

Centralized validation logic for Slack integration:
- Case type detection (asks FIRST if not detected)
- Context extraction (preserves what's mentioned in message)
- Missing field detection (only missing fields, don't re-ask)
- Kong Gateway service fetching
"""
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from openai import AsyncOpenAI

from app.services.slack.case_selector import CaseSelector
from app.schemas.chat_schemas import ChatContextSchema
from app.core.config import settings
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.geo_loc_mst_model import GeoLocMstModel
from app.db.models.services_mst_model import ServicesMstModel
from app.core.enum import ServiceTypeEnum
import logging
import json

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of validation and context collection."""
    # Case information
    case_code: Optional[str] = None
    case_type_code: Optional[str] = None
    service_type: Optional[str] = None

    # Context
    context: Optional[ChatContextSchema] = None
    collected_fields: Optional[Dict[str, Any]] = None
    missing_fields: Optional[List[str]] = None

    # Special cases
    needs_case_type_selection: bool = False
    needs_kong_service_selection: bool = False
    kong_services: Optional[List[Dict[str, Any]]] = None

    # Message
    message: Optional[str] = None


class SlackValidationService:
    """Centralized validation service for Slack integration.

    Validation flow:
    1. Detect case type FIRST - if not detected, prompt for case type
    2. Extract context from message (preserves what's mentioned)
    3. Check only for missing fields (don't re-ask what's already collected)
    4. For Kong Gateway, fetch services from database
    """

    def __init__(self, db: AsyncSession, tenant_code: str):
        """Initialize validation service.

        Args:
            db: Database session
            tenant_code: Tenant code
        """
        self.db = db
        self.tenant_code = tenant_code
        self.case_selector = CaseSelector(db)

    async def validate_and_collect_context(
        self,
        user_message: str,
        user_code: str
    ) -> ValidationResult:
        """Perform one-time validation and context collection.

        Args:
            user_message: User's message
            user_code: User code

        Returns:
            ValidationResult with all collected information
        """
        # Step 1: Detect case FIRST
        case_result = await self.case_selector.detect_case(
            user_message=user_message,
            tenant_code=self.tenant_code
        )
        logger.info(f"Case detection result: {case_result}")

        # If no case detected, prompt for case type FIRST
        if not case_result.get("case_code") or case_result.get("case_code") == "general_chat":
            logger.info("No case detected, prompting for case type FIRST")
            return ValidationResult(
                needs_case_type_selection=True,
                message="What would you like to create? (S3 bucket, SQS queue, DynamoDB table, or Kong Gateway route?)"
            )

        # Step 2: Extract context from message (preserves what's mentioned)
        extracted = await self._extract_context_from_message(user_message)
        logger.info(f"Extracted context: {extracted}")

        # Step 3: Build context with extracted values (null if not mentioned)
        # Convert environment string to EnvironmentEnum
        environment_enum = None
        if extracted.get("environment"):
            from app.core.enum import EnvironmentEnum

            env_str = extracted.get("environment").lower()
            # Map various string forms to EnvironmentEnum values
            env_mapping = {
                "development": EnvironmentEnum.dev,
                "dev": EnvironmentEnum.dev,
                "staging": EnvironmentEnum.staging,
                "stage": EnvironmentEnum.staging,
                "production": EnvironmentEnum.prod,
                "prod": EnvironmentEnum.prod,
            }
            environment_enum = env_mapping.get(env_str)
            if environment_enum:
                logger.info(f"Mapped environment string '{env_str}' to {environment_enum}")
            else:
                logger.warning(f"Unknown environment string: {env_str}")

        context = ChatContextSchema(
            tenants_mst_code=self.tenant_code,
            user_mst_code=user_code,
            applications_mst_code=extracted.get("product_code"),  # null if not found
            resource_group_mst_code=extracted.get("resource_group_code"),  # null if not found
            services_mst_code=extracted.get("service_code"),  # null if not found
            environment_enum=environment_enum,  # EnvironmentEnum or None
            geo_loc_mst_code=extracted.get("geo_loc_code"),  # null if not found
            case_type_ref_code=case_result.get("case_type_code"),
            infra_vendor_enum="aws"  # Default to AWS
        )

        # Step 4: Check only MISSING fields (don't re-ask what's already collected)
        missing = self._get_missing_fields(context, case_result.get("case_type_code"))
        logger.info(f"Missing fields: {missing}")

        # Step 5: Kong Gateway special handling
        if case_result.get("case_type_code") == "kong_gateway":
            # Fetch Kong Gateway services from database
            if context.applications_mst_code:
                kong_services = await self._fetch_kong_gateway_services(
                    context.applications_mst_code
                )

                # If no service selected and services available, prompt for selection
                if not context.services_mst_code and kong_services:
                    logger.info(f"Kong Gateway: found {len(kong_services)} services, prompting for selection")
                    return ValidationResult(
                        needs_kong_service_selection=True,
                        kong_services=kong_services,
                        context=context,
                        case_code=case_result.get("case_code"),
                        case_type_code=case_result.get("case_type_code"),
                        service_type=case_result.get("service_type", "gateway"),
                        collected_fields=extracted,
                        missing_fields=missing  # Keep track of what's still missing
                    )
            else:
                # No application selected yet - need to prompt for it first
                logger.info("Kong Gateway: no application selected, will prompt for it")

        # Return final result
        return ValidationResult(
            context=context,
            case_code=case_result.get("case_code"),
            case_type_code=case_result.get("case_type_code"),
            service_type=case_result.get("service_type"),
            collected_fields=extracted,
            missing_fields=missing  # Only fields NOT yet collected
        )

    def _get_missing_fields(
        self,
        context: ChatContextSchema,
        case_type_code: Optional[str] = None
    ) -> List[str]:
        """Check which required fields are missing.

        Only checks for presence - returns list of missing field names.

        Args:
            context: Chat context
            case_type_code: Case type code (for Kong Gateway check)

        Returns:
            List of missing field names
        """
        missing = []

        # Always required
        if not context.applications_mst_code:
            missing.append("application")
        if not context.geo_loc_mst_code:
            missing.append("geographic location")
        if not context.environment_enum:
            missing.append("environment")

        # Kong Gateway specific: service is required
        if case_type_code == "kong_gateway" and not context.services_mst_code:
            missing.append("service")

        return missing

    async def _extract_context_from_message(self, message: str) -> Dict[str, Any]:
        """Use LLM to extract infrastructure context from user message.

        Returns dict with optional fields: product_code, environment, geo_loc_code, etc.
        Preserves what's mentioned, returns null for what's not mentioned.

        Args:
            message: User message

        Returns:
            Dict with extracted context (null for missing fields)
        """
        try:
            client = AsyncOpenAI(api_key=settings.openai_api_key)

            # Get available products for this tenant
            stmt = select(ApplicationsMstModel).where(
                ApplicationsMstModel.tenants_mst_code == self.tenant_code,
                ApplicationsMstModel.is_deleted == False
            )
            result = await self.db.execute(stmt)
            products = result.scalars().all()
            product_list = "\n".join([f"- {p.name} (code: {p.code})" for p in products])

            prompt = f"""You are a strict JSON extraction tool. Extract ONLY explicitly mentioned infrastructure context from: "{message}"

Available products:
{product_list}

CRITICAL RULES:
1. Return ONLY a valid JSON object (no markdown, no code blocks)
2. Use null for ANY field not EXPLICITLY mentioned in the message
3. Do NOT guess, infer, or assume values - if not mentioned, return null
4. Product must be mentioned by name (e.g., "core", "Finance", "HR Payroll")
5. Environment keywords: "prod"→"production", "stage"/"staging"→"staging", "dev"/"development"→"development"
6. Geographic location must be explicitly mentioned (e.g., "ap-south-1", "london", "new york")

Required format:
{{
  "product_code": "UUID-from-list-above-or-null",
  "environment": "development|staging|production|null",
  "geo_loc_code": "location-code-or-null"
}}

Examples:
- "for core in prod" → {{"product_code": "178d48fc-8b2c-4e79-aca9-e18f089a05f9", "environment": "production", "geo_loc_code": null}}
- "in staging" → {{"product_code": null, "environment": "staging", "geo_loc_code": null}}
- "for prod" → {{"product_code": null, "environment": "production", "geo_loc_code": null}}

Return ONLY the JSON object."""

            response = await client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.1,
                response_format={"type": "json_object"}
            )

            result_text = response.choices[0].message.content.strip()
            logger.debug(f"AI extraction result: {result_text}")
            extracted = json.loads(result_text)

            # Validation: Check if extracted product name actually appears in message
            # Also check for multiple product matches
            if extracted.get("product_code"):
                message_lower = message.lower()
                product_found = False
                matched_products = []

                # Find ALL products that match the message
                for product in products:
                    product_name_lower = product.name.lower()
                    if product_name_lower in message_lower or any(word in product_name_lower for word in message_lower.split()):
                        matched_products.append(product)
                        if product.code == extracted["product_code"]:
                            product_found = True
                            logger.debug(f"Validated product '{product.name}' found in message")

                if len(matched_products) > 1:
                    # Multiple products match
                    extracted["product_code"] = None
                    extracted["product_matches"] = [
                        {"code": p.code, "name": p.name} for p in matched_products
                    ]
                    logger.info(f"Found {len(matched_products)} product matches")
                elif len(matched_products) == 1:
                    # Single match
                    extracted["product_code"] = matched_products[0].code
                elif not product_found:
                    logger.warning(f"AI hallucinated product_code {extracted['product_code']}")
                    extracted["product_code"] = None

            # Validation: Map geo_loc name/text to actual geo_loc code
            if extracted.get("geo_loc_code"):
                geo_loc_input = extracted["geo_loc_code"].lower()

                # Search for matching geo_loc
                stmt = select(GeoLocMstModel).where(
                    GeoLocMstModel.tenants_mst_code == self.tenant_code,
                    GeoLocMstModel.is_deleted == False
                )
                result = await self.db.execute(stmt)
                geo_locs = result.scalars().all()

                # Find ALL matching geo_locs
                matched_geo_locs = []
                for geo_loc in geo_locs:
                    if (geo_loc_input in geo_loc.name.lower() or
                        geo_loc.name.lower() in geo_loc_input or
                        geo_loc_input in geo_loc.code.lower()):
                        matched_geo_locs.append(geo_loc)
                        logger.debug(f"Matched geo_loc '{geo_loc.name}' (code: {geo_loc.code})")

                if len(matched_geo_locs) == 1:
                    extracted["geo_loc_code"] = matched_geo_locs[0].code
                elif len(matched_geo_locs) > 1:
                    # Multiple matches
                    extracted["geo_loc_code"] = None
                    extracted["geo_loc_matches"] = [
                        {"code": g.code, "name": g.name} for g in matched_geo_locs
                    ]
                    logger.info(f"Found {len(matched_geo_locs)} geo_loc matches for '{geo_loc_input}'")
                else:
                    logger.warning(f"Could not find geo_loc matching '{extracted['geo_loc_code']}'")
                    extracted["geo_loc_code"] = None

            return extracted

        except Exception as e:
            logger.error(f"Failed to extract context: {str(e)}", exc_info=True)
            return {}  # Return empty dict if extraction fails

    async def _fetch_kong_gateway_services(
        self,
        application_code: str
    ) -> List[Dict[str, Any]]:
        """Fetch services for Kong Gateway dropdown.

        Filters by:
        - applications_mst_code
        - service_type = API
        - is_public_facing = True
        - is_deleted = False

        Args:
            application_code: Application code

        Returns:
            List of service dicts with 'code' and 'name' fields
        """
        try:
            stmt = select(ServicesMstModel).where(
                ServicesMstModel.applications_mst_code == application_code,
                ServicesMstModel.service_type == ServiceTypeEnum.API,
                ServicesMstModel.is_public_facing == True,
                ServicesMstModel.is_deleted == False
            )
            result = await self.db.execute(stmt)
            services = result.scalars().all()

            logger.info(f"Found {len(services)} Kong Gateway services for application {application_code}")

            return [
                {
                    "code": s.code,
                    "name": s.name
                }
                for s in services
            ]

        except Exception as e:
            logger.error(f"Failed to fetch Kong Gateway services: {str(e)}", exc_info=True)
            return []

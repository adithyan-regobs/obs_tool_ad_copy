"""
Service for sales inquiry form and email validation
"""
from typing import Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.sales_inquiry_repository import SalesInquiryRepository
from app.schemas.sales_inquiry_schemas import SalesInquiryRequest
import logging

logger = logging.getLogger(__name__)


class SalesInquiryService:
    """Service for handling email validation and sales inquiry submissions"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.inquiry_repo = SalesInquiryRepository(db)

    async def validate_email(self, email: str) -> Dict[str, Any]:
        """
        Validate whether an email is eligible for signup.

        Current logic: basic @ check (always returns not eligible).
        This will be elaborated in the future with:
        - Domain allowlists
        - Invitation checks
        - Admin-approved email lists

        Returns:
            dict with eligible (bool), email (str), message (str)
        """
        email = email.lower().strip()

        if not email or '@' not in email:
            # @ check fails → not eligible → show sales inquiry form
            logger.info(f"[SALES] Email validation failed for: {email} — missing @")
            return {
                "eligible": False,
                "email": email,
                "message": "Please fill out the inquiry form below and our sales team will contact you."
            }

        # Email has valid @ → eligible for signup
        # This validation will be elaborated in future (domain checks, allowlists, etc.)
        logger.info(f"[SALES] Email validation passed for: {email}")

        return {
            "eligible": True,
            "email": email,
            "message": "Email is eligible for signup."
        }

    async def create_inquiry(self, request: SalesInquiryRequest) -> Dict[str, Any]:
        """
        Create a new sales inquiry from the form submission.

        Returns:
            dict with success (bool), message (str), inquiry_code (str)
        """
        logger.info(f"[SALES] Creating sales inquiry for: {request.email}")

        inquiry = await self.inquiry_repo.create_inquiry(
            full_name=request.full_name,
            email=request.email,
            phone_number=request.phone_number,
            company_name=request.company_name,
            role_title=request.role_title,
            team_department=request.team_department,
            country=request.country,
            use_case=request.use_case,
            requirement_description=request.requirement_description,
            preferred_contact_method=request.preferred_contact_method,
            referral_source=request.referral_source,
            additional_notes=request.additional_notes,
            consent_given=request.consent_given,
        )

        await self.db.commit()

        logger.info(f"[SALES] Sales inquiry created: {inquiry.code} for {request.email}")

        return {
            "success": True,
            "message": "Thank you for your interest! Our sales team will contact you soon.",
            "inquiry_code": inquiry.code,
        }

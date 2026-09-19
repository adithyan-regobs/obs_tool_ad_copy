"""
Repository for sales_inquiries table operations
"""
from typing import Optional, List
import uuid
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.base_repository import BaseRepository
from app.db.models.sales_inquiry_model import SalesInquiryModel


class SalesInquiryRepository(BaseRepository[SalesInquiryModel]):
    """Repository for sales_inquiries table"""

    def __init__(self, session: AsyncSession):
        super().__init__(SalesInquiryModel, session)

    async def create_inquiry(
        self,
        full_name: str,
        email: str,
        country: str,
        use_case: str,
        requirement_description: str,
        consent_given: bool,
        phone_number: Optional[str] = None,
        company_name: Optional[str] = None,
        role_title: Optional[str] = None,
        team_department: Optional[str] = None,
        preferred_contact_method: Optional[str] = None,
        referral_source: Optional[str] = None,
        additional_notes: Optional[str] = None,
    ) -> SalesInquiryModel:
        """
        Create a new sales inquiry record.

        Maps full_name to BaseModel.name and requirement_description to BaseModel.description.
        """
        inquiry_code = str(uuid.uuid4())

        return await self.create(
            code=inquiry_code,
            name=full_name,
            description=requirement_description,
            email=email,
            phone_number=phone_number,
            company_name=company_name,
            role_title=role_title,
            team_department=team_department,
            country=country,
            use_case=use_case,
            preferred_contact_method=preferred_contact_method,
            referral_source=referral_source,
            additional_notes=additional_notes,
            consent_given=consent_given,
            status="new",
            is_active=True,
            is_deleted=False,
        )

    async def get_by_email(self, email: str) -> List[SalesInquiryModel]:
        """Get all inquiries for an email address, newest first"""
        stmt = select(SalesInquiryModel).where(
            and_(
                SalesInquiryModel.email == email,
                SalesInquiryModel.is_deleted == False,
            )
        ).order_by(SalesInquiryModel.created_at.desc())

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

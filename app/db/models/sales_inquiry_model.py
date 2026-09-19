"""
Database model for sales inquiries from unknown users during signup
"""
from sqlalchemy import Column, String, Text, Boolean
from app.db.models.base_model import BaseModel


class SalesInquiryModel(BaseModel):
    """
    Sales Inquiry table for tracking interest from unknown/unapproved users.

    When a user tries to sign up but fails email validation,
    they are shown a sales inquiry form. Submitted data is stored here
    for the sales team to follow up.

    BaseModel provides: id, code, name (Full Name), description (Requirement),
    created_at, updated_at, is_deleted, is_active
    """

    __tablename__ = "sales_inquiries"

    # Contact details
    email = Column(String(255), nullable=False, index=True, comment="User email address")
    phone_number = Column(String(50), nullable=True, comment="Phone number")

    # Organization details
    company_name = Column(String(255), nullable=True, comment="Company or organization name")
    role_title = Column(String(255), nullable=True, comment="Role or job title")
    team_department = Column(String(255), nullable=True, comment="Team or department")
    country = Column(String(100), nullable=False, comment="Country")

    # Interest details
    use_case = Column(String(1000), nullable=False, comment="What they want to use DevLift.ai for")
    # 'description' from BaseModel is used for "Briefly describe your requirement"

    # Additional info
    preferred_contact_method = Column(String(50), nullable=True, comment="Email, Phone, or Either")
    referral_source = Column(String(255), nullable=True, comment="How they heard about DevLift.ai")
    additional_notes = Column(Text, nullable=True, comment="Any additional notes")

    # Consent
    consent_given = Column(Boolean, nullable=False, default=False, comment="Agreed to be contacted")

    # Sales tracking
    status = Column(
        String(50),
        default="new",
        nullable=False,
        index=True,
        comment="Inquiry status: new, contacted, qualified, closed"
    )

    def __repr__(self):
        return (
            f"<SalesInquiry(code='{self.code}', email='{self.email}', "
            f"status='{self.status}', name='{self.name}')>"
        )

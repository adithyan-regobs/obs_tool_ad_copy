"""
Schemas for sales inquiry form and email validation
"""
from pydantic import BaseModel, Field, validator
from typing import Optional


class ValidateEmailRequest(BaseModel):
    """Request schema for email validation during signup"""
    email: str = Field(..., description="Email address to validate")

    @validator('email')
    def validate_email(cls, v):
        """Basic email validation - will be elaborated in future"""
        if not v or '@' not in v:
            raise ValueError('Invalid email address')
        return v.lower().strip()


class ValidateEmailResponse(BaseModel):
    """Response schema for email validation"""
    eligible: bool = Field(..., description="Whether the email is eligible for signup")
    email: str = Field(..., description="Normalized email address")
    message: str = Field(..., description="Human-readable message")


class SalesInquiryRequest(BaseModel):
    """Request schema for submitting a sales inquiry form"""
    full_name: str = Field(..., min_length=1, max_length=255, description="Full name")
    email: str = Field(..., description="Email address")
    phone_number: Optional[str] = Field(None, max_length=50, description="Phone number")
    company_name: Optional[str] = Field(None, max_length=255, description="Company / Organization name")
    role_title: Optional[str] = Field(None, max_length=255, description="Role / Job title")
    team_department: Optional[str] = Field(None, max_length=255, description="Team / Department")
    country: str = Field(..., min_length=1, max_length=100, description="Country")
    use_case: str = Field(..., min_length=1, max_length=1000, description="What do you want to use DevLift.ai for?")
    requirement_description: str = Field(..., min_length=1, max_length=2000, description="Briefly describe your requirement")
    preferred_contact_method: Optional[str] = Field(None, max_length=50, description="Email, Phone, or Either")
    referral_source: Optional[str] = Field(None, max_length=255, description="How did you hear about DevLift.ai?")
    additional_notes: Optional[str] = Field(None, description="Additional notes")
    consent_given: bool = Field(..., description="I agree to be contacted by the DevLift.ai team")

    @validator('email')
    def validate_email(cls, v):
        if not v or '@' not in v:
            raise ValueError('Invalid email address')
        return v.lower().strip()

    @validator('consent_given')
    def validate_consent(cls, v):
        if not v:
            raise ValueError('You must agree to be contacted by the DevLift.ai team')
        return v


class SalesInquiryResponse(BaseModel):
    """Response schema for sales inquiry submission"""
    success: bool
    message: str
    inquiry_code: str

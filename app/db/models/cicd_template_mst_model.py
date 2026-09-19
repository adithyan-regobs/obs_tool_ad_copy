from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class CicdTemplateMstModel(BaseModel):
    """
    CI/CD Template Master Table
    Stores predefined CI/CD workflow templates with configurable steps.
    """
    __tablename__ = "cicd_template_mst"

    # Foreign Keys
    tenant_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Tenant this template belongs to (null = global/system template)"
    )

    applications_mst_code = Column(
        String(100),
        ForeignKey("services_mst.code", ondelete="CASCADE"),
        nullable=True,
        comment="Application/service this template is associated with (null = universal template)"
    )

    # Template identification
    name = Column(
        String(255),
        nullable=False,
        comment="Display name of the CI/CD template (e.g., 'Quick Release', 'Full Release')"
    )

    # Template configuration with all steps
    config = Column(
        JSONB,
        nullable=False,
        comment="Workflow template configuration including steps and their settings"
    )

    # Example config structure:
    # {
    #   "language": "go",
    #   "steps": [
    #     {
    #       "id": "code-checkout",
    #       "name": "Code Checkout",
    #       "order": 1,
    #       "mandatory": true,
    #       "enabled": true,
    #       "category": "setup"
    #     },
    #     {
    #       "id": "code-quality-check",
    #       "name": "Code Quality Check",
    #       "order": 4,
    #       "mandatory": false,
    #       "enabled": true,
    #       "category": "quality"
    #     }
    #   ],
    #   "workflow_triggers": {
    #     "push_branches": ["main"],
    #     "manual_trigger": true
    #   }
    # }

    # Additional metadata
    description = Column(
        Text,
        nullable=True,
        comment="Description of when to use this template"
    )

    template_type = Column(
        String(50),
        nullable=False,
        default="custom",
        comment="Template type: quick_release, full_release, custom"
    )

    is_active = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether this template is active and available for use"
    )

    is_system_template = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether this is a system template (cannot be deleted)"
    )

    # Individual step fields (used when storing each step as a separate row)
    step_order = Column(
        Integer,
        nullable=True,
        comment="Execution order of the step"
    )

    step_category = Column(
        String(50),
        nullable=True,
        comment="Step category: setup, quality, build, notification"
    )

    step_enabled = Column(
        Boolean,
        default=True,
        nullable=False,
        comment="Whether the step is enabled"
    )

    step_mandatory = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether the step is mandatory"
    )

    step_dependencies = Column(
        JSONB,
        nullable=True,
        comment="Array of step codes this step depends on"
    )

    language = Column(
        String(50),
        nullable=True,
        comment="Programming language for the workflow (go, java, nodejs, python)"
    )

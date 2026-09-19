from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.models.base_model import BaseModel


class LanguageRefModel(BaseModel):
    """
    Language Reference table.
    Stores programming language configurations for pipeline execution.

    Each language version stores YAML templates for multiple CI/CD platforms
    in a single JSONB column, eliminating duplicate rows.
    """

    __tablename__ = "language_ref"

    # Language version (e.g., "3.12", "20.x", "17")
    version = Column(
        String(50),
        nullable=False,
    )

    # YAML templates for different CI/CD platforms (JSONB)
    # Example: {"github_actions": "/templates/github-actions/python-3.12.yml",
    #           "gitlab_ci": "/templates/gitlab-ci/python-3.12.yml"}
    yaml_templates = Column(
        JSONB,
        nullable=False,
    )

    # Relationship to PipelineMstModel
    pipelines = relationship(
        "PipelineMstModel",
        back_populates="language_ref",
    )

    # Relationship to ServiceConfigModel
    service_configs = relationship(
        "ServiceConfigModel",
        back_populates="language_ref",
    )

    def __repr__(self):
        platforms = ', '.join(self.yaml_templates.keys()) if self.yaml_templates else 'None'
        return (
            f"<LanguageRef(id={self.id}, code='{self.code}', "
            f"name='{self.name}', version='{self.version}', "
            f"platforms=[{platforms}])>"
        )

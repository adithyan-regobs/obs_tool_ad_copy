from sqlalchemy import Column, String, ForeignKey, Enum as SqlEnum, Boolean
from app.db.models.base_model import BaseModel
from app.core.enum import EnvironmentEnum, AWSResourceTypeEnum


class AWSSecretsParametersMstModel(BaseModel):
    """
    Model for storing AWS Secrets Manager and SSM Parameter Store references.

    Naming convention for full_resource_path:
    {tenant_name}/{environment}/{region}/{service_name}/{secret_name}

    Examples:
    - acme-corp/prod/us-east-1/payment-service/database-password
    - acme-corp/staging/us-west-2/user-service/api-key
    - techstart/dev/ap-south-1/auth-service (when name is not provided)
    """
    __tablename__ = "aws_secrets_parameters_mst"

    # Foreign Keys
    tenants_mst_code = Column(
        String(100),
        ForeignKey("tenants_mst.code", ondelete="CASCADE"),
        nullable=False,
    )
    applications_mst_code = Column(
        String(100),
        ForeignKey("applications_mst.code", ondelete="CASCADE"),
        nullable=False,
        comment="Application code for canvas-scoped variables"
    )

    # Generic resource identifier (services_mst.code or infrastructure_mst.code)
    resource_code = Column(
        String(100),
        nullable=False,
        comment="Generic resource code — services_mst.code or infrastructure_mst.code"
    )
    resource_type_str = Column(
        String(50),
        nullable=False,
        comment="Canvas resource type: service, database, bucket, queue, function, etc."
    )

    # AWS Resource Info
    resource_type = Column(
        SqlEnum(AWSResourceTypeEnum, name="aws_resource_type_enum"),
        nullable=False,
        comment="Type of AWS resource: secret (Secrets Manager) or parameter (SSM Parameter Store)"
    )

    full_resource_path = Column(
        String(500),
        nullable=False,
        comment="Full AWS resource path following pattern: tenant/env/region/service/name"
    )

    resource_arn = Column(
        String(500),
        nullable=True,
        comment="AWS ARN of the resource (populated after creation in AWS)"
    )

    # Environment
    environments_enum = Column(
        SqlEnum(EnvironmentEnum, name="environment_enum"),
        nullable=False,
        comment="Environment: dev, staging, or prod"
    )

    # Parameter Type (only for SSM Parameters)
    parameter_type = Column(
        String(50),
        nullable=True,
        comment="Parameter type for SSM: String, StringList, or SecureString. Null for Secrets Manager."
    )

    # Deletion Tracking
    is_secret_deleted = Column(
        Boolean,
        default=False,
        nullable=False,
        comment="True if secret/parameter deleted in AWS but kept in DB for tracking"
    )

    def __repr__(self):
        return (
            f"<AWSSecretsParametersMst(id={self.id}, code='{self.code}', "
            f"type='{self.resource_type.value}', path='{self.full_resource_path}')>"
        )

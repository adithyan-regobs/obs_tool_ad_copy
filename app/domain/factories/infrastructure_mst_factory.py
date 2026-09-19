from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from uuid import uuid4
from app.core.enum import DeploymentStatusEnum, EnvironmentEnum
from app.core.config import settings
from app.mcp_servers.devlift_mcp.meta_data.registry import is_paas_tenant
from app.domain.validators import infra_config_validator as infra_config


def resolve_infra_name_env(tenant_code: str, environment: Any) -> str:
    """The `env` segment a resource NAME is built from.

    PaaS tenants all deploy into the single onboarding environment, so the
    name carries that constant and the deployment environment is a separate
    axis. Enterprise tenants run real per-environment accounts, and their
    terraform layers branch on the resource's OWN env — `onboarding_default_env`
    ("trial") is not a value that appears anywhere in their infrastructure.
    """
    if is_paas_tenant(tenant_code):
        return settings.onboarding_default_env
    return str(getattr(environment, "value", environment) or "")


def _sqs_base_name(
    identifier: str,
    tenant_code: str,
    env: str,
    region_code: str,
    index: str,
) -> str:
    """The queue's local.name — the name before the module's `.fifo` append.

    Enterprise tenants own their naming and their layers do NOT prefix:
    layers/queue is `prod ? "{identifier}-{env}-{index}" : "{identifier}-{index}"`.
    Building the PaaS prefix for them produced a name no queue has ever had in
    AWS, and the URL and ARN derived from it were wrong with it.
    """
    if not is_paas_tenant(tenant_code):
        if env == "prod":
            return f"{identifier}-{env}-{index}"
        return f"{identifier}-{index}"
    if env == "prod":
        return f"{tenant_code}-{env}-{region_code}-{index}-{identifier}"
    return f"{tenant_code}-{region_code}-{index}-{identifier}"


def resolve_sqs_name(
    identifier: str,
    tenant_code: str,
    env: str,
    region_code: str,
    index: str,
    fifo_queue: bool = False,
) -> str:
    """Mirror the final AWS queue name produced by:
      1. the queue layer's local.name — see _sqs_base_name, which carries both
         the PaaS and the enterprise formula.
      2. terraform-aws-modules/sqs/aws — when fifo_queue=true, appends ".fifo"
         unconditionally.
    Enabling DLQ does NOT change the main queue name.
    """
    base = _sqs_base_name(identifier, tenant_code, env, region_code, index)
    return f"{base}.fifo" if fifo_queue else base


def resolve_sqs_dlq_name(
    identifier: str,
    tenant_code: str,
    env: str,
    region_code: str,
    index: str,
    fifo_queue: bool = False,
) -> str:
    """Mirror terraform-aws-modules/sqs/aws default DLQ naming:
        dlq = "{main_queue_local_name}-dlq" + ".fifo" if fifo.
    `main_queue_local_name` is the name BEFORE the upstream module's `.fifo`
    append, i.e. just our wrapper's local.name.
    """
    dlq = f"{_sqs_base_name(identifier, tenant_code, env, region_code, index)}-dlq"
    return f"{dlq}.fifo" if fifo_queue else dlq


def resolve_sqs_urls(queue_name: str, region: str, account_id: str) -> Tuple[str, str]:
    """Return (queue_url, queue_arn) for the given resolved queue name."""
    queue_url = f"https://sqs.{region}.amazonaws.com/{account_id}/{queue_name}"
    queue_arn = f"arn:aws:sqs:{region}:{account_id}:{queue_name}"
    return queue_url, queue_arn

def make_infrastructure_mst_eks(
    identifier: str,
    tenant_code: str,
    environment: EnvironmentEnum,
    region: str,
    infrastructuretype_ref_code: str,
    infra_vendor_accounts_mst_code: str,
    locator: Dict[str, Any],
    application_code: Optional[str] = None,
    resource_group_mst_code: Optional[str] = None,
    infra_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    infra_status_updated_by: Optional[str] = None,
    geo_loc_mst_code: str = "region-aspora-mumbai",
) -> Dict[str, Any]:
    """Factory to build an InfrastructureMst domain object for EKS clusters."""
    infra_code = f"INFRA_EKS_{uuid4().hex[:8].upper()}"
    name = f"EKS Cluster - {identifier}"

    return {
        "code": infra_code,
        "name": name,
        "description": f"EKS cluster {identifier} in {region} for {tenant_code}/{application_code}",
        "infrastructuretype_ref_code": infrastructuretype_ref_code,
        "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code,
        "resource_group_mst_code": resource_group_mst_code,
        "tenants_mst_code": tenant_code,
        "applications_mst_code": application_code,
        "environments_enum": environment,
        "geo_loc_mst_code": geo_loc_mst_code,
        "locator": locator,
        "infra_status": infra_status,
        "infra_status_updated_by": infra_status_updated_by,
        "infra_status_updated_at": datetime.utcnow() if infra_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
    }


def make_infrastructure_mst_ec2(
    identifier: str,
    tenant_code: str,
    application_code: str,
    environment: EnvironmentEnum,
    region: str,
    infrastructuretype_ref_code: str,
    infra_vendor_accounts_mst_code: str,
    resource_group_mst_code: Optional[str] = None,
    infra_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    infra_status_updated_by: Optional[str] = None,
    geo_loc_mst_code: str = "region-aspora-mumbai"
) -> Dict[str, Any]:
    """
    Factory to build an InfrastructureMst domain object for EC2 instances.

    Args:
        identifier: EC2 instance name/identifier
        tenant_code: Tenant code
        application_code: Application code
        environment: Environment enum
        region: AWS region (e.g., "ap-south-1")
        infrastructuretype_ref_code: Infrastructure type code (e.g., "ecs_ec2_infrastructuretype_ref")
        infra_vendor_accounts_mst_code: Vendor account code
        resource_group_mst_code: Optional resource group code
        infra_status: Initial deployment status (default: INITIATED)
        gitops_workflow_id: Optional workflow ID
        resource_identifier: Optional AWS ARN
        infra_status_updated_by: Optional user/system that created this resource
        geo_loc_mst_code: Geographic location code

    Returns:
        Dictionary ready for repository.create()
    """
    infra_code = f"INFRA_EC2_{uuid4().hex[:8].upper()}"
    name = f"EC2 Instance - {identifier}"
    locator = {
        "instance_name": identifier,
        "region": region
    }

    return {
        "code": infra_code,
        "name": name,
        "description": f"EC2 instance {identifier} in {region} for {tenant_code}/{application_code}",
        "infrastructuretype_ref_code": infrastructuretype_ref_code,
        "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code,
        "resource_group_mst_code": resource_group_mst_code,
        "tenants_mst_code": tenant_code,
        "applications_mst_code": application_code,
        "environments_enum": environment,
        "geo_loc_mst_code": geo_loc_mst_code,
        "locator": locator,
        "infra_status": infra_status,
        "infra_status_updated_by": infra_status_updated_by,
        "infra_status_updated_at": datetime.utcnow() if infra_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
    }


# The leftover config a factory copies into the locator verbatim is filtered by
# infra_config_validator.passthrough_extra. Every `extra` below spreads LAST, so
# without that filter a caller supplying `bucket_name`, `queue_arn` or
# `accountId` overwrites the value the factory just computed and repoints the
# row at a resource of their choosing — in another AWS account, in the
# accountId case.


def make_infrastructure_mst_s3(
    config: Dict[str, Any],
    tenant_code: str,
    application_code: str,
    environment: EnvironmentEnum,
    region: str,
    infrastructuretype_ref_code: str,
    infra_vendor_accounts_mst_code: str,
    resource_group_mst_code: Optional[str] = None,
    infra_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    infra_status_updated_by: Optional[str] = None,
    geo_loc_mst_code: str = "region-aspora-mumbai"
) -> Dict[str, Any]:
    """
    Factory to build an InfrastructureMst domain object for S3 buckets.

    Args:
        identifier: S3 bucket name/identifier
        tenant_code: Tenant code (from JWT or request)
        application_code: Application code
        environment: Environment enum (dev, staging, prod)
        region: AWS region (e.g., "ap-south-1")
        infrastructuretype_ref_code: Infrastructure type code (e.g., "s3_infrastructuretype_ref")
        infra_vendor_accounts_mst_code: Vendor account code (e.g., "aws_vance_dev")
        resource_group_mst_code: Optional resource group code
        infra_status: Initial deployment status (default: INITIATED)
        gitops_workflow_id: Optional workflow ID if linking to existing workflow
        resource_identifier: Optional AWS ARN (set after vendor creation)
        infra_status_updated_by: Optional user/system that created this resource

    Returns:
        Dictionary ready for repository.create()

    Example:
        bucket_data = make_infrastructure_mst_s3(
            identifier="my-app-logs",
            tenant_code="vance",
            application_code="payment_app",
            environment=EnvironmentEnum.DEV,
            region="ap-south-1",
            infrastructuretype_ref_code="s3_infrastructuretype_ref",
            infra_vendor_accounts_mst_code="aws_vance_dev",
            creation_status=DeploymentStatusEnum.INITIATED
        )
        bucket = await repo.create(**bucket_data)

    This isolates object creation logic (like defaults, code generation, and derived fields)
    from the repository and service layers.
    """
    # Destructure required fields; everything else goes straight into locator as-is
    identifier = config.get("identifier", "")
    extra = infra_config.passthrough_extra(config, infrastructuretype_ref_code)

    # Generate unique code for this infrastructure resource
    infra_code = f"INFRA_S3_{uuid4().hex[:8].upper()}"

    # Resolve the actual AWS bucket name using the same convention as the S3
    # Terraform layer (infrastructure/layers/aws/v1/bucket/main.tf):
    #   {organization}-{env}-{region_code}-{index}-{identifier}
    env = settings.onboarding_default_env
    region_code = settings.onboarding_default_region_code
    index = settings.onboarding_default_index
    aws_bucket_name = f"{tenant_code}-{env}-{region_code}-{index}-{identifier}"
    bucket_arn = f"arn:aws:s3:::{aws_bucket_name}"

    # Build descriptive name — enterprise tenants own their naming, use identifier directly
    name = aws_bucket_name if is_paas_tenant(tenant_code) else identifier

    # Build locator JSONB. bucket_name = resolved AWS name; identifier = raw
    # user input (used for form hydration and duplicate validation).
    locator = {
        "bucket_name": aws_bucket_name,
        "identifier": identifier,
        "bucket_arn": bucket_arn,
        "region": region,
        **extra,
    }

    # resource_group_mst_code is optional - can be None
    # The infrastructure_mst table allows NULL for resource_group_mst_code

    # Return dictionary for repository create method
    infrastructure_data = {
        "code": infra_code,
        "name": name,
        "description": f"S3 bucket {aws_bucket_name} in {region} for {tenant_code}/{application_code}",
        # Foreign Keys (Required)
        "infrastructuretype_ref_code": infrastructuretype_ref_code,
        "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code,
        "resource_group_mst_code": resource_group_mst_code,
        "tenants_mst_code": tenant_code,
        "applications_mst_code": application_code,
        "environments_enum": environment,
        "geo_loc_mst_code": geo_loc_mst_code,
        # Resource Identity
        "locator": locator,
        # GitOps Workflow Tracking
        "infra_status": infra_status,
        "infra_status_updated_by": infra_status_updated_by,
        "infra_status_updated_at": datetime.utcnow() if infra_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
    }

    return infrastructure_data


def make_infrastructure_mst_sqs(
    config: Dict[str, Any],
    tenant_code: str,
    application_code: str,
    environment: EnvironmentEnum,
    region: str,
    infrastructuretype_ref_code: str,
    infra_vendor_accounts_mst_code: str,
    resource_group_mst_code: Optional[str] = None,
    infra_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    infra_status_updated_by: Optional[str] = None,
    geo_loc_mst_code: str = "region-aspora-mumbai"
) -> Dict[str, Any]:
    """
    Factory to build an InfrastructureMst domain object for SQS queues.

    Args:
        identifier: SQS queue name/identifier
        tenant_code: Tenant code (from JWT or request)
        application_code: Application code
        environment: Environment enum (dev, staging, prod)
        region: AWS region (e.g., "ap-south-1")
        infrastructuretype_ref_code: Infrastructure type code (e.g., "sqs_infrastructuretype_ref")
        infra_vendor_accounts_mst_code: Vendor account code (e.g., "aws_vance_dev")
        resource_group_mst_code: Optional resource group code
        infra_status: Initial deployment status (default: INITIATED)
        gitops_workflow_id: Optional workflow ID if linking to existing workflow
        resource_identifier: Optional AWS ARN (set after vendor creation)
        infra_status_updated_by: Optional user/system that created this resource

    Returns:
        Dictionary ready for repository.create()

    Example:
        queue_data = make_infrastructure_mst_sqs(
            identifier="core-queue-1",
            tenant_code="vance",
            application_code="payment_app",
            environment=EnvironmentEnum.DEV,
            region="ap-south-1",
            infrastructuretype_ref_code="sqs_infrastructuretype_ref",
            infra_vendor_accounts_mst_code="aws_vance_dev",
            infra_status=DeploymentStatusEnum.INITIATED
        )
        queue = await repo.create(**queue_data)

    This isolates object creation logic (like defaults, code generation, and derived fields)
    from the repository and service layers.
    """
    # Destructure required fields; everything else goes straight into locator as-is
    identifier = config.get("identifier", "")
    extra = infra_config.passthrough_extra(config, infrastructuretype_ref_code)

    # Generate unique code for this infrastructure resource
    infra_code = f"INFRA_SQS_{uuid4().hex[:8].upper()}"

    # Resolve the actual AWS queue name (and derived URL/ARN) using the same
    # convention as the Terraform SQS layer + upstream module FIFO suffix.
    # Booleans may arrive as "true"/"false" strings from form state.
    def _as_bool(v):
        return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes")
    fifo_queue = _as_bool(extra.get("fifo_queue", False))
    create_dlq = _as_bool(extra.get("create_dlq", False))
    env = resolve_infra_name_env(tenant_code, environment)
    index = settings.onboarding_default_index
    sqs_region_code = settings.onboarding_default_region_code
    aws_queue_name = resolve_sqs_name(
        identifier, tenant_code, env, sqs_region_code, index, fifo_queue=fifo_queue,
    )
    account_id = extra.get("accountId") or settings.onboarding_default_account_id
    queue_url, queue_arn = resolve_sqs_urls(aws_queue_name, region, account_id)

    # Build descriptive name — enterprise tenants own their naming, use identifier directly
    name = aws_queue_name if is_paas_tenant(tenant_code) else identifier

    # Build locator JSONB. queue_name = resolved AWS name (source of truth for
    # IAM / URL construction). identifier = raw user input (for edit hydration
    # and duplicate validation). When create_dlq is true, also record the
    # upstream-module-default DLQ identifiers so IAM / UI can reference it.
    locator = {
        "queue_name": aws_queue_name,
        "identifier": identifier,
        "queue_url": queue_url,
        "queue_arn": queue_arn,
        "region": region,
        **extra,
    }
    if create_dlq:
        dlq_name = resolve_sqs_dlq_name(
            identifier, tenant_code, env, sqs_region_code, index, fifo_queue=fifo_queue,
        )
        dlq_url, dlq_arn = resolve_sqs_urls(dlq_name, region, account_id)
        locator["dlq_name"] = dlq_name
        locator["dlq_url"] = dlq_url
        locator["dlq_arn"] = dlq_arn

    # resource_group_mst_code is optional - can be None
    # The infrastructure_mst table allows NULL for resource_group_mst_code

    # Return dictionary for repository create method
    infrastructure_data = {
        "code": infra_code,
        "name": name,
        "description": f"SQS queue {aws_queue_name} in {region} for {tenant_code}/{application_code}",
        # Foreign Keys (Required)
        "infrastructuretype_ref_code": infrastructuretype_ref_code,
        "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code,
        "resource_group_mst_code": resource_group_mst_code,
        "tenants_mst_code": tenant_code,
        "applications_mst_code": application_code,
        "environments_enum": environment,
        "geo_loc_mst_code": geo_loc_mst_code,
        # Resource Identity
        "locator": locator,
        # GitOps Workflow Tracking
        "infra_status": infra_status,
        "infra_status_updated_by": infra_status_updated_by,
        "infra_status_updated_at": datetime.utcnow() if infra_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
    }

    return infrastructure_data


def make_infrastructure_mst_dynamodb(
    config: Dict[str, Any],
    tenant_code: str,
    application_code: str,
    environment: EnvironmentEnum,
    region: str,
    infrastructuretype_ref_code: str,
    infra_vendor_accounts_mst_code: str,
    resource_group_mst_code: Optional[str] = None,
    infra_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    infra_status_updated_by: Optional[str] = None,
    geo_loc_mst_code: str = "region-aspora-mumbai"
) -> Dict[str, Any]:
    """
    Factory to build an InfrastructureMst domain object for DynamoDB tables.

    Args:
        identifier: DynamoDB table name/identifier
        partition_key: Partition key attribute name
        partition_key_type: Partition key type (S, N, or B)
        tenant_code: Tenant code (from JWT or request)
        application_code: Application code
        environment: Environment enum (dev, staging, prod)
        region: AWS region (e.g., "ap-south-1")
        infrastructuretype_ref_code: Infrastructure type code (e.g., "dynamodb_infrastructuretype_ref")
        infra_vendor_accounts_mst_code: Vendor account code (e.g., "aws_vance_dev")
        resource_group_mst_code: Optional resource group code
        infra_status: Initial deployment status (default: INITIATED)
        gitops_workflow_id: Optional workflow ID if linking to existing workflow
        resource_identifier: Optional AWS ARN (set after vendor creation)
        infra_status_updated_by: Optional user/system that created this resource

    Returns:
        Dictionary ready for repository.create()

    Example:
        table_data = make_infrastructure_mst_dynamodb(
            identifier="user-sessions",
            partition_key="user_id",
            partition_key_type="S",
            tenant_code="vance",
            application_code="auth_service",
            environment=EnvironmentEnum.DEV,
            region="ap-south-1",
            infrastructuretype_ref_code="dynamodb_infrastructuretype_ref",
            infra_vendor_accounts_mst_code="aws_vance_dev",
            infra_status=DeploymentStatusEnum.INITIATED
        )
        table = await repo.create(**table_data)

    This isolates object creation logic (like defaults, code generation, and derived fields)
    from the repository and service layers.
    """
    # Destructure required fields; everything else goes straight into locator as-is
    identifier = config.get("identifier", "")
    partition_key = config.get("partition_key", "")
    partition_key_type = config.get("partition_key_type", "")
    extra = infra_config.passthrough_extra(
        config, infrastructuretype_ref_code,
        also_exclude=("partition_key", "partition_key_type"),
    )

    # Generate unique code for this infrastructure resource
    infra_code = f"INFRA_DYNAMODB_{uuid4().hex[:8].upper()}"

    # Resolve the actual AWS table name using the same convention as the
    # DynamoDB Terraform layer:
    #   PaaS       {organization}-{env}-{region_code}-{index}-{identifier}
    #   enterprise {identifier}-{index}    — layers/dynamo prefixes nothing
    #
    # The enterprise branch is not cosmetic: the post-action publishes
    # locator.table_name to services as DYNAMODB_TABLE_NAME, so the prefixed
    # form pointed them at a table that does not exist in AWS.
    env = settings.onboarding_default_env
    region_code = settings.onboarding_default_region_code
    index = settings.onboarding_default_index
    aws_table_name = (
        f"{tenant_code}-{env}-{region_code}-{index}-{identifier}"
        if is_paas_tenant(tenant_code)
        else f"{identifier}-{index}"
    )

    # Build descriptive name — enterprise tenants own their naming, use identifier directly
    name = aws_table_name if is_paas_tenant(tenant_code) else identifier

    # Build locator JSONB — required fields first, then all extra config fields
    locator = {
        "table_name": aws_table_name,
        "identifier": identifier,
        "partition_key": partition_key,
        "partition_key_type": partition_key_type,
        "region": region,
        **extra,
    }

    # resource_group_mst_code is optional - can be None
    # The infrastructure_mst table allows NULL for resource_group_mst_code

    # Return dictionary for repository create method
    infrastructure_data = {
        "code": infra_code,
        "name": name,
        "description": f"DynamoDB table {aws_table_name} with partition key {partition_key} ({partition_key_type}) in {region} for {tenant_code}/{application_code}",
        # Foreign Keys (Required)
        "infrastructuretype_ref_code": infrastructuretype_ref_code,
        "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code,
        "resource_group_mst_code": resource_group_mst_code,
        "tenants_mst_code": tenant_code,
        "applications_mst_code": application_code,
        "environments_enum": environment,
        "geo_loc_mst_code": geo_loc_mst_code,
        # Resource Identity
        "locator": locator,
        # GitOps Workflow Tracking
        "infra_status": infra_status,
        "infra_status_updated_by": infra_status_updated_by,
        "infra_status_updated_at": datetime.utcnow() if infra_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
    }

    return infrastructure_data


def make_infrastructure_mst_redis(
    config: Dict[str, Any],
    tenant_code: str,
    application_code: str,
    environment: EnvironmentEnum,
    region: str,
    infrastructuretype_ref_code: str,
    infra_vendor_accounts_mst_code: str,
    resource_group_mst_code: Optional[str] = None,
    infra_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    infra_status_updated_by: Optional[str] = None,
    geo_loc_mst_code: str = "region-aspora-mumbai"
) -> Dict[str, Any]:
    """Factory to build an InfrastructureMst domain object for ElastiCache Redis clusters."""
    extra = infra_config.passthrough_extra(config, infrastructuretype_ref_code)

    infra_code = f"INFRA_REDIS_{uuid4().hex[:8].upper()}"

    # Resolve the actual AWS replication group name using the same convention
    # as S3/SQS/DynamoDB factories: 
    #   {organization}-{env}-{region_code}-{index}-{identifier}
    env = settings.onboarding_default_env
    region_code = settings.onboarding_default_region_code
    index = settings.onboarding_default_index
    prefix = f"{tenant_code}-{env}-{region_code}-{index}-"

    # `redis_cluster_name` is the RESOLVED name, and it is also an accepted
    # input — hydration and clone paths send back what they read. Prefixing it
    # again turns "aspora-...-cache" into "aspora-...-aspora-...-cache", which
    # then sticks, so the prefix is stripped before it is used as the raw name.
    identifier = config.get("identifier") or config.get("redis_cluster_name", "")
    if identifier and not config.get("identifier") and identifier.startswith(prefix):
        identifier = identifier[len(prefix):]

    redis_cluster_name = f"{prefix}{identifier}"
    user_id = f"{identifier}-iam"
    port = str(config.get("port") or 6379)

    name = redis_cluster_name if is_paas_tenant(tenant_code) else identifier

    locator = {
        "redis_cluster_name": redis_cluster_name,
        "identifier": identifier,
        "user_id": user_id,
        "port": port,
        "region": region,
        **extra,
    }

    return {
        "code": infra_code,
        "name": name,
        "description": f"Redis cluster {redis_cluster_name} in {region} for {tenant_code}/{application_code}",
        "infrastructuretype_ref_code": infrastructuretype_ref_code,
        "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code,
        "resource_group_mst_code": resource_group_mst_code,
        "tenants_mst_code": tenant_code,
        "applications_mst_code": application_code,
        "environments_enum": environment,
        "geo_loc_mst_code": geo_loc_mst_code,
        "locator": locator,
        "infra_status": infra_status,
        "infra_status_updated_by": infra_status_updated_by,
        "infra_status_updated_at": datetime.utcnow() if infra_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
    }


def make_infrastructure_mst_aurora_postgres(
    config: Dict[str, Any],
    tenant_code: str,
    application_code: str,
    environment: EnvironmentEnum,
    region: str,
    infrastructuretype_ref_code: str,
    infra_vendor_accounts_mst_code: str,
    resource_group_mst_code: Optional[str] = None,
    infra_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    infra_status_updated_by: Optional[str] = None,
    geo_loc_mst_code: str = "region-aspora-mumbai"
) -> Dict[str, Any]:
    """Factory to build an InfrastructureMst domain object for Aurora PostgreSQL clusters."""
    server_name = config.get("db_server_name") or config.get("identifier", "")
    extra = infra_config.passthrough_extra(config, infrastructuretype_ref_code)

    infra_code = f"INFRA_AURORA_PG_{uuid4().hex[:8].upper()}"
    name = server_name

    locator = {
        "db_server_name": server_name,
        "region": region,
        **extra,
    }

    return {
        "code": infra_code,
        "name": name,
        "description": f"Aurora PostgreSQL cluster {server_name} in {region} for {tenant_code}/{application_code}",
        "infrastructuretype_ref_code": infrastructuretype_ref_code,
        "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code,
        "resource_group_mst_code": resource_group_mst_code,
        "tenants_mst_code": tenant_code,
        "applications_mst_code": application_code,
        "environments_enum": environment,
        "geo_loc_mst_code": geo_loc_mst_code,
        "locator": locator,
        "infra_status": infra_status,
        "infra_status_updated_by": infra_status_updated_by,
        "infra_status_updated_at": datetime.utcnow() if infra_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
    }


def make_infrastructure_mst_aurora_mysql(
    config: Dict[str, Any],
    tenant_code: str,
    application_code: str,
    environment: EnvironmentEnum,
    region: str,
    infrastructuretype_ref_code: str,
    infra_vendor_accounts_mst_code: str,
    resource_group_mst_code: Optional[str] = None,
    infra_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    infra_status_updated_by: Optional[str] = None,
    geo_loc_mst_code: str = "region-aspora-mumbai"
) -> Dict[str, Any]:
    """Factory to build an InfrastructureMst domain object for Aurora MySQL clusters."""
    server_name = config.get("db_server_name") or config.get("identifier", "")
    extra = infra_config.passthrough_extra(config, infrastructuretype_ref_code)

    infra_code = f"INFRA_AURORA_MYSQL_{uuid4().hex[:8].upper()}"
    name = server_name

    locator = {
        "db_server_name": server_name,
        "region": region,
        **extra,
    }

    return {
        "code": infra_code,
        "name": name,
        "description": f"Aurora MySQL cluster {server_name} in {region} for {tenant_code}/{application_code}",
        "infrastructuretype_ref_code": infrastructuretype_ref_code,
        "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code,
        "resource_group_mst_code": resource_group_mst_code,
        "tenants_mst_code": tenant_code,
        "applications_mst_code": application_code,
        "environments_enum": environment,
        "geo_loc_mst_code": geo_loc_mst_code,
        "locator": locator,
        "infra_status": infra_status,
        "infra_status_updated_by": infra_status_updated_by,
        "infra_status_updated_at": datetime.utcnow() if infra_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
    }


def make_infrastructure_mst_k8s_postgres(
    config: Dict[str, Any],
    tenant_code: str,
    application_code: str,
    environment: EnvironmentEnum,
    infrastructuretype_ref_code: str,
    infra_vendor_accounts_mst_code: str,
    resource_group_mst_code: Optional[str] = None,
    infra_status: DeploymentStatusEnum = DeploymentStatusEnum.INITIATED,
    gitops_workflow_id: Optional[int] = None,
    resource_identifier: Optional[str] = None,
    infra_status_updated_by: Optional[str] = None,
    geo_loc_mst_code: str = "region-aspora-mumbai"
) -> Dict[str, Any]:
    """Factory to build an InfrastructureMst domain object for K8s PostgreSQL servers."""
    identifier = config.get("identifier") or config.get("server_name", "")
    extra = infra_config.passthrough_extra(config, infrastructuretype_ref_code)

    infra_code = f"INFRA_K8S_PG_{uuid4().hex[:8].upper()}"
    name = identifier

    locator = {
        "server_name": identifier,
        **extra,
    }

    return {
        "code": infra_code,
        "name": name,
        "description": f"Kubernetes PostgreSQL server {identifier} for {tenant_code}/{application_code}",
        "infrastructuretype_ref_code": infrastructuretype_ref_code,
        "infra_vendor_accounts_mst_code": infra_vendor_accounts_mst_code,
        "resource_group_mst_code": resource_group_mst_code,
        "tenants_mst_code": tenant_code,
        "applications_mst_code": application_code,
        "environments_enum": environment,
        "geo_loc_mst_code": geo_loc_mst_code,
        "locator": locator,
        "infra_status": infra_status,
        "infra_status_updated_by": infra_status_updated_by,
        "infra_status_updated_at": datetime.utcnow() if infra_status else None,
        "gitops_workflow_id": gitops_workflow_id,
        "resource_identifier": resource_identifier,
    }

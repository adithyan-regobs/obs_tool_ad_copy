"""
Aspora Config Enrichment Helper

Enriches config_snapshot with derived AWS infrastructure values.
This is Aspora/Vance tenant-specific logic.
"""

import logging
from typing import Dict
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def enrich_config_with_infrastructure_values(
    db: AsyncSession,
    config_snapshot: Dict,
    tenant_code: str
) -> None:
    """
    Enrich config_snapshot with values from infrastructure_mst.locator.

    This is Aspora/Vance-specific logic that:
    1. Fetches infrastructure record using infrastructure_mst_code
    2. Extracts values from infrastructure.locator JSONB
    3. Uses naming strategy to derive ECR repository, ECS service name, etc.
    4. Enriches config_snapshot in-place with derived values

    Enriched values:
    - ecr_repository: Full ECR URI
    - aws_region: AWS region code
    - aws_account_id: AWS account ID
    - ecs_cluster: ECS cluster name
    - aws_role_arn: IAM role ARN
    - ecs_service: ECS service name with -svc suffix
    - index: Version index

    Args:
        db: Database session
        config_snapshot: Config snapshot dict to enrich (modified in-place)
        tenant_code: Tenant code for naming strategy
    """
    from app.utils.naming_strategies import get_naming_strategy
    from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
    from app.repository.geo_loc_mst_repository import GeoLocMstRepository
    from app.repository.language_ref_repository import LanguageRefRepository

    # Resolve language_ref_code -> language_name/version. Downstream script-gen
    # components read config_snapshot['language_name'], but only language_ref_code
    # is stored on the service config, so it must be derived here.
    if not config_snapshot.get('language_name'):
        language_ref_code = config_snapshot.get('language_ref_code')
        if language_ref_code:
            language_ref = await LanguageRefRepository(db).get_by_code(language_ref_code)
            if language_ref:
                config_snapshot['language_name'] = language_ref.name
                config_snapshot.setdefault('language_version', language_ref.version)
                logger.info(
                    f"Resolved language from language_ref_code={language_ref_code}: "
                    f"name={language_ref.name}, version={language_ref.version}"
                )
            else:
                logger.warning(f"Language reference not found for code: {language_ref_code}")
        else:
            logger.warning("No language_ref_code in config_snapshot, cannot resolve language_name")

    infrastructure_mst_code = config_snapshot.get('infrastructure_mst_code')
    if not infrastructure_mst_code:
        logger.warning("No infrastructure_mst_code in config_snapshot, skipping enrichment")
        return

    # Fetch infrastructure record
    infra_repo = InfrastructureMstRepository(db)
    infrastructure = await infra_repo.get_by_code(infrastructure_mst_code)

    if not infrastructure:
        logger.warning(f"Infrastructure not found: {infrastructure_mst_code}, skipping enrichment")
        return

    # Extract values from infrastructure.locator JSONB field
    infrastructure_config = infrastructure.locator or {}
    aws_region = infrastructure_config.get("region")
    account_id = infrastructure_config.get("account_id")
    cluster_name = infrastructure_config.get("cluster_name")
    index = infrastructure_config.get("index", "01")

    if not aws_region or not account_id:
        logger.warning(f"Missing region or account_id in infrastructure.locator for {infrastructure_mst_code}")
        return

    # Get geo_loc for naming
    geo_loc_mst_code = config_snapshot.get('geo_loc_mst_code', '')
    geo_loc_repo = GeoLocMstRepository(db)
    geo_loc_mst = await geo_loc_repo.get_by_code(geo_loc_mst_code)
    geo_loc_name_for_naming = geo_loc_mst.name.lower() if geo_loc_mst else geo_loc_mst_code

    # Get service details
    service_name = config_snapshot.get('service_name', '')
    # Remove -service suffix if present (will be re-added by naming strategy)
    if service_name.lower().endswith('-service'):
        service_name = service_name[:-8]  # Remove last 8 characters ("-service")
        logger.info(f"Removed -service suffix from service_name: {config_snapshot.get('service_name', '')} → {service_name}")
    environment = config_snapshot.get('environment', '')
    product_name = config_snapshot.get('product_name', 'core')

    # Use naming strategy to derive ECR and other AWS resources
    naming_strategy = get_naming_strategy(tenant_code)

    # Generate org name
    application_name = product_name  # Using product_name as application_name
    logger.info(f"DEBUG: product_name={product_name}, application_name={application_name}")
    org_name = naming_strategy.generate_org_name(
        tenant_code=tenant_code,
        application_name=application_name
    )
    logger.info(f"DEBUG: org_name generated = {org_name}")

    # Normalize environment for naming (staging -> stage)
    env_for_naming = "stage" if environment == "staging" else environment

    # Generate ECR repo name
    ecr_repo_name = naming_strategy.generate_ecr_repo_name(
        org_name=org_name,
        environment=env_for_naming,
        geo_loc_mst_code=geo_loc_name_for_naming,
        index=index,
        service_name=service_name
    )

    # Generate ECR URI
    ecr_uri = naming_strategy.generate_ecr_uri(
        account_id=account_id,
        aws_region=aws_region,
        ecr_repo_name=ecr_repo_name
    )

    # Generate IAM role ARN
    iam_role_arn = naming_strategy.generate_iam_role_arn(account_id=account_id)

    # Generate ECS service name (pass raw application_name, let the method handle org_name internally)
    logger.info(f"DEBUG: Calling generate_ecs_service_name with application_name={application_name}")
    ecs_service = naming_strategy.generate_ecs_service_name(
        application_name=application_name,  # Pass raw application name (e.g., "core"), method will convert to org_name
        environment=env_for_naming,
        geo_loc_mst_code=geo_loc_name_for_naming,
        index=index,
        service_name=service_name
    )
    logger.info(f"DEBUG: ecs_service generated = {ecs_service}")

    # Enrich config_snapshot with derived values
    config_snapshot['ecr_repository'] = ecr_uri
    config_snapshot['aws_region'] = aws_region
    config_snapshot['aws_account_id'] = account_id
    config_snapshot['ecs_cluster'] = cluster_name
    config_snapshot['aws_role_arn'] = iam_role_arn
    config_snapshot['ecs_service'] = ecs_service
    config_snapshot['index'] = index
    config_snapshot['org_name'] = org_name

    logger.info(f"Enriched config_snapshot with infrastructure values (Aspora-specific):")
    logger.info(f"  - org_name: {org_name}")
    logger.info(f"  - ecr_repo_name: {ecr_repo_name}")
    logger.info(f"  - ecr_repository: {ecr_uri}")
    logger.info(f"  - ecs_service: {ecs_service}")
    logger.info(f"  - aws_region: {aws_region}")
    logger.info(f"  - ecs_cluster: {cluster_name}")
    logger.info(f"  - aws_role_arn: {iam_role_arn}")

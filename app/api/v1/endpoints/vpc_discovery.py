"""
API endpoints for VPC Discovery.
Provides endpoints to discover VPCs, subnets, and resources across AWS accounts.
"""
import logging
from typing import Tuple, List, Dict, Any, Optional
from fastapi import APIRouter, HTTPException, Depends, Query, Body
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import aioboto3

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.core.enum import InfraVendorEnum, EnvironmentEnum
from app.repository.infra_vendor_accounts_mst_repository import InfraVendorAccountsMstRepository
from app.services.vpc_discovery_service import VPCDiscoveryService
from app.services.vpc_and_resource_discovery_service import VpcAndResourceDiscoveryService
from app.schemas.vpc_discovery_schemas import (
    SingleVPCResponse,
    MultiAccountResponse,
    AccountsListResponse,
    AccountIdsResponse,
    AccountIdInfo,
    VPCInfo,
    AWSAccountInfo,
    CanvasApiResponse,
    CanvasVariablesResponse,
)


class ScanVpcsRequest(BaseModel):
    """Request body for multi-account VPC scanning"""
    accounts: Dict[str, List[str]]  # account_id -> list of vpc_ids

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory cache for VPC discovery results
# Key: "{tenant_code}:{account_ids}:{regions}:{role_name}"
_vpc_cache: Dict[str, Any] = {}


async def get_aws_auth_config(
    db: AsyncSession,
    tenant_code: str,
    environment: EnvironmentEnum = EnvironmentEnum.prod,
    application_code: Optional[str] = None
) -> dict:
    """
    Get AWS auth_config from tenant's vendor account using hierarchical lookup.

    Lookup priority (most specific first):
    1. Application-level account (within tenant)
    2. Tenant-level account (fallback)

    Args:
        db: Database session
        tenant_code: Tenant code
        environment: Environment enum (default: prod)
        application_code: Optional application code for hierarchical lookup

    Returns:
        auth_config dict from infra_vendor_accounts_mst

    Raises:
        HTTPException: If no AWS account configured for tenant
    """
    repo = InfraVendorAccountsMstRepository(db)
    vendor_account = await repo.get_by_hierarchy(
        tenant_code=tenant_code,
        infra_vendor_enum=InfraVendorEnum.aws,
        environments_enum=environment,
        application_code=application_code
    )

    if not vendor_account or not vendor_account.auth_config:
        raise HTTPException(
            status_code=403,
            detail="AWS account not configured for this tenant. Please configure AWS credentials in DevOps settings."
        )

    return vendor_account.auth_config


@router.get("/vpc/{vpc_identifier:path}", response_model=SingleVPCResponse, summary="Get Single VPC Details")
async def get_single_vpc(
    vpc_identifier: str,
    region: str = Query(..., description="AWS Region where the VPC resides (e.g., us-east-1)"),
    environment: EnvironmentEnum = Query(EnvironmentEnum.prod, description="Environment for AWS credentials lookup"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all subnets and resources for a single VPC.

    Security:
        - JWT authentication required
        - Uses tenant's AWS credentials from infra_vendor_accounts_mst

    This endpoint discovers the complete VPC topology including:
    - Internet Gateway and NAT Gateways
    - Public and private subnets (classified by route table)
    - Resources in each subnet (EC2, RDS, Lambda, ECS, Load Balancers)
    - Security groups and their rules

    **Parameters:**
    - `vpc_identifier`: VPC ID (vpc-xxx) or VPC ARN
    - `region`: AWS region where the VPC is located
    - `environment`: Environment for AWS credentials (dev, staging, prod)

    **Response:**
    Complete VPC details with subnets grouped by public/private classification.

    **Use Case:**
    - Visualize VPC network topology
    - Audit security group configurations
    - Inventory resources across subnets
    """
    try:
        user, tenant = user_and_tenant

        # Get AWS auth_config from tenant's vendor account
        auth_config = await get_aws_auth_config(db, tenant.code, environment)

        # Create service and get VPC details
        service = VPCDiscoveryService(auth_config)
        result = await service.get_vpc_details(vpc_identifier, region)
        return result

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/account/{account_id}/vpc/{vpc_id}", response_model=SingleVPCResponse, summary="Get VPC from Specific Account")
async def get_vpc_by_account(
    account_id: str,
    vpc_id: str,
    region: str = Query(..., description="AWS Region where the VPC resides"),
    role_name: str = Query("OrganizationAccountAccessRole", description="IAM role name for cross-account access"),
    environment: EnvironmentEnum = Query(EnvironmentEnum.prod, description="Environment for AWS credentials lookup"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get VPC details from a specific AWS account using cross-account role assumption.

    Security:
        - JWT authentication required
        - Uses tenant's AWS credentials with AssumeRole to target account

    This endpoint is for multi-account environments where you need to discover
    VPCs in linked accounts (e.g., AWS Organizations member accounts).

    **Parameters:**
    - `account_id`: Target AWS Account ID (12 digits)
    - `vpc_id`: VPC ID in the target account
    - `region`: AWS region
    - `role_name`: IAM role to assume in target account (default: OrganizationAccountAccessRole)
    - `environment`: Environment for base AWS credentials

    **Prerequisites:**
    - The target account must have the specified IAM role
    - The role must trust your base account
    - The role must have permissions to describe VPC resources

    **Response:**
    Complete VPC details from the target account. 
    """
    try:
        user, tenant = user_and_tenant

        # Get AWS auth_config from tenant's vendor account
        auth_config = await get_aws_auth_config(db, tenant.code, environment)

        # Create service and get VPC details from specific account
        service = VPCDiscoveryService(auth_config)
        result = await service.get_vpc_by_account(account_id, vpc_id, region, role_name)
        return result

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accounts", response_model=MultiAccountResponse, summary="Discover VPCs Across Multiple Accounts")
async def get_all_accounts_vpcs(
    account_ids: str = Query(..., description="Comma-separated AWS Account IDs"),
    regions: str = Query("us-east-1", description="Comma-separated AWS regions to scan"),
    role_name: str = Query("OrganizationAccountAccessRole", description="IAM role name for cross-account access"),
    environment: EnvironmentEnum = Query(EnvironmentEnum.prod, description="Environment for AWS credentials lookup"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all VPCs across multiple AWS accounts and regions.

    Security:
        - JWT authentication required
        - Uses tenant's AWS credentials with AssumeRole to each target account

    This endpoint is designed for multi-account AWS environments (e.g., AWS Organizations)
    to discover and inventory all VPCs across member accounts.

    **Parameters:**
    - `account_ids`: Comma-separated list of AWS Account IDs (e.g., "123456789012,987654321098")
    - `regions`: Comma-separated list of AWS regions to scan (e.g., "us-east-1,eu-west-1")
    - `role_name`: IAM role to assume in each target account
    - `environment`: Environment for base AWS credentials

    **Response:**
    ```json
    {
        "aws_account": [
            {
                "account_id": "123456789012",
                "account_name": "prod-main",
                "vpcs": [...]
            }
        ]
    }
    ```

    **Caching:**
    Results are cached in-memory after first fetch to avoid long wait times (~2+ minutes).
    """
    try:
        user, tenant = user_and_tenant

        # Parse comma-separated values
        account_list = [a.strip() for a in account_ids.split(",") if a.strip()]
        region_list = [r.strip() for r in regions.split(",") if r.strip()]

        if not account_list:
            raise HTTPException(status_code=400, detail="At least one account_id is required")
        if not region_list:
            raise HTTPException(status_code=400, detail="At least one region is required")

        # Build cache key
        cache_key = f"{tenant.code}:{','.join(sorted(account_list))}:{','.join(sorted(region_list))}:{role_name}"

        # Return cached result if available
        if cache_key in _vpc_cache:
            return _vpc_cache[cache_key]

        # Get AWS auth_config from tenant's vendor account
        auth_config = await get_aws_auth_config(db, tenant.code, environment)

        # Create service and discover VPCs across accounts
        service = VPCDiscoveryService(auth_config)
        result = await service.get_all_accounts_vpcs(account_list, region_list, role_name)

        # Store in cache
        _vpc_cache[cache_key] = result

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/accounts/list", response_model=AccountsListResponse, summary="List AWS Accounts")
async def list_accounts(
    environment: EnvironmentEnum = Query(EnvironmentEnum.prod, description="Environment for AWS credentials lookup"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    List all AWS accounts from AWS Organizations.

    Security:
        - JWT authentication required
        - Uses tenant's AWS credentials

    This endpoint returns a lightweight list of AWS accounts without VPC details.
    Use this to get account IDs for the other VPC discovery endpoints.

    **Prerequisites:**
    - Your AWS account must be part of an AWS Organization
    - Credentials must have organizations:ListAccounts permission

    **Response:**
    ```json
    {
        "accounts": [
            {
                "account_id": "123456789012",
                "account_name": "prod-main",
                "email": "aws-prod@company.com",
                "status": "ACTIVE"
            }
        ],
        "total": 5
    }
    ```
    """
    try:
        user, tenant = user_and_tenant

        # Get AWS auth_config from tenant's vendor account
        auth_config = await get_aws_auth_config(db, tenant.code, environment)

        # Create service and list accounts
        service = VPCDiscoveryService(auth_config)
        result = await service.list_accounts()
        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health", summary="Health Check")
async def health_check():
    """
    Health check endpoint for VPC Discovery service.

    No authentication required.
    """
    return {"status": "healthy", "service": "vpc-discovery", "version": "1.0.0"}


@router.get("/account-ids", response_model=AccountIdsResponse, summary="List Account IDs")
async def list_account_ids(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    List unique account IDs from configured vendor accounts.

    Security:
        - JWT authentication required
        - Returns only accounts for the authenticated tenant

    This endpoint extracts account IDs from the auth_config of all
    infra_vendor_accounts_mst records for the tenant.

    Account ID is extracted from:
    1. 'account_id' field in auth_config (if present)
    2. Parsed from 'assume_role_arn' (format: arn:aws:iam::ACCOUNT_ID:role/...)

    **Response:**
    ```json
    {
        "account_ids": ["123456789012", "987654321098"],
        "accounts": [
            {
                "account_id": "123456789012",
                "vendor": "aws",
                "environment": "prod"
            }
        ],
        "total": 2
    }
    ```
    """
    try:
        user, tenant = user_and_tenant

        # Get all vendor accounts for tenant from repository
        repo = InfraVendorAccountsMstRepository(db)
        vendor_accounts = await repo.get_all_by_tenant(tenant.code)

        # Use service to extract unique account IDs
        unique_ids, accounts_details = VPCDiscoveryService.get_unique_account_ids(vendor_accounts)

        return AccountIdsResponse(
            account_ids=unique_ids,
            accounts=[AccountIdInfo(**acc) for acc in accounts_details],
            total=len(unique_ids)
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/scan-vpcs", response_model=CanvasApiResponse, summary="Discover VPCs Across Multiple Accounts")
async def scan_vpcs_by_ids(
    application_code: str = Query(..., description="Application code for the VPC discovery"),
    environment: EnvironmentEnum = Query(EnvironmentEnum.prod, description="Environment for AWS credentials lookup"),
    accounts_vpcs: Dict[str, List[str]] = Body(..., description="Multi-account format: {account_id: [vpc_ids]}"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Discover VPCs across multiple AWS accounts and return flat, normalized structure for canvas visualization.

    **Request Format:**
    ```
    POST /scan-vpcs?application_code=app1&environment=prod
    Body: {
        "597189966628": ["vpc-12345", "vpc-67890"],
        "123456789012": ["vpc-abc12"]
    }
    ```

    **Authentication Flow:**
    - Lambda IAM Role → AssumeRole(account1_assume_role_arn) → Scan VPCs in account1
    - Lambda IAM Role → AssumeRole(account2_assume_role_arn) → Scan VPCs in account2

    **Auth Config Structure Required (in DB):**
    ```json
    {
        "authentication_type": "iam_role",
        "accounts": [
            {
                "account_id": "597189966628",
                "region": "ap-south-1",
                "assume_role_arn": "arn:aws:iam::597189966628:role/AssumeRole"
            }
        ]
    }
    ```

    Security:
        - JWT authentication required
        - Uses Lambda IAM role to assume per-account roles from auth_config

    **Parameters:**
    - `application_code`: Application identifier (required)
    - `environment`: Environment for AWS credentials (dev, staging, prod)
    - `accounts_vpcs`: Multi-account format with account IDs mapping to VPC IDs

    **Response Structure:**
    Returns flat, normalized structure optimized for canvas visualization:
    - `geoLocations`: Geographic regions (Asia, Europe, North America, etc.)
    - `accounts`: Cloud provider accounts with vendor info
    - `cloudRegions`: AWS regions within accounts
    - `vpcs`: VPCs with CIDR blocks and references to cloud regions
    - `subnets`: Public and private subnets with references to VPCs
    - `nodes`: Discovered resources (EC2, RDS, Lambda, ECS, Load Balancers)
    - `nodeVariables`: Empty object (for future use)
    - `variableRefs`: Empty object (for future use)
    - `securityGroupRules`: Empty array (for future use)
    """
    try:
        user, tenant = user_and_tenant

        if not accounts_vpcs:
            raise HTTPException(status_code=400, detail="At least one account with VPC IDs is required")

        # Get AWS auth_config from tenant's vendor account with hierarchical lookup
        auth_config = await get_aws_auth_config(db, tenant.code, environment, application_code)

        # Validate multi-account structure
        if "accounts" not in auth_config or not isinstance(auth_config["accounts"], list):
            raise HTTPException(
                status_code=400,
                detail="Multi-account auth_config required. Please configure accounts array in auth_config."
            )

        # Create service and discover VPCs across accounts
        service = VPCDiscoveryService(auth_config)
        multi_account_response = await service.discover_vpcs_multi_account(auth_config, accounts_vpcs, application_code)

        # Transform to canvas structure for frontend
        canvas_response = VPCDiscoveryService.transform_to_canvas_response(multi_account_response)

        return canvas_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to discover VPCs: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fetch_vpc_and_resources", response_model=CanvasApiResponse, summary="Fetch VPC and Resources for Canvas")
async def fetch_vpc_and_resources(
    application_code: str = Query(..., description="Application code"),
    environment: EnvironmentEnum = Query(EnvironmentEnum.prod, description="Environment (dev, stage, qa, prod)"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch pre-configured VPC and resource data for canvas visualization.

    Returns a flat, normalized `CanvasApiResponse` for the given tenant,
    application and environment — ready for ProjectCanvas on the frontend.

    **Parameters:**
    - `application_code`: Application identifier (e.g., core, falcon)
    - `environment`: Deployment environment (dev, stage, qa, prod)

    **Response Structure:**
    - `geoLocations`: Geographic groupings
    - `accounts`: Cloud provider accounts
    - `cloudRegions`: AWS/GCP/Azure regions
    - `vpcs`: VPCs with CIDR and region references
    - `subnets`: Public/private subnets per VPC
    - `nodes`: Discovered resources (EC2, RDS, Lambda, ECS, etc.)
    - `nodeVariables`: Per-node variable definitions
    - `variableRefs`: Cross-node variable references
    - `securityGroupRules`: Security group rules
    """
    try:
        user, tenant = user_and_tenant

        service = VpcAndResourceDiscoveryService()
        data = await service.get_canvas_data(
            tenant_code=tenant.code,
            application_code=application_code,
            environment=environment.value,
            db=db,
        )

        if data is None:
            raise HTTPException(
                status_code=404,
                detail=f"No canvas data found for tenant='{tenant.code}', application='{application_code}', environment='{environment.value}'",
            )

        return data

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch canvas data: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/available-vendors")
async def get_available_vendors(
    environment: str = Query(..., description="Environment (dev, stage, qa, prod)"),
    application_code: Optional[str] = Query(None, description="Application code for hierarchical lookup"),
    db: AsyncSession = Depends(get_db),
    current_user_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Returns distinct cloud vendors available for the tenant/environment/application combo.
    Uses hierarchical lookup: application-level first, then tenant-level fallback.
    """
    _, tenant = current_user_tenant
    try:
        env_enum = EnvironmentEnum(environment)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid environment: {environment}")

    repo = InfraVendorAccountsMstRepository(db)
    vendors = await repo.get_distinct_vendors_by_hierarchy(
        tenant_code=tenant.code,
        environments_enum=env_enum,
        application_code=application_code,
    )
    return {"vendors": vendors}

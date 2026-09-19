"""
Infrastructure Master API Endpoints

API endpoints for fetching infrastructure instances (clusters, compute resources).
"""
from typing import Tuple, Optional, List, Dict, Any
from fastapi import HTTPException, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.core.authz.security import AuthenticationOnly, SecureRouter
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.infrastructure_mst_service import InfrastructureMstService
from app.schemas.infrastructure_schemas import (
    InfrastructureStatusResponse,
    InfrastructureDetailRequest,
    InfrastructureDetailResponse,
    CloneInfraSettingsRequest,
)
from app.core.enum import EnvironmentEnum, ResourceStatusEnum
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository

router = SecureRouter()


class InfrastructureListItem(BaseModel):
    """Response schema for infrastructure list item"""
    code: str
    name: str
    infrastructuretype_ref_code: str
    environment: str
    geo_loc_mst_code: Optional[str] = None
    resource_identifier: Optional[str] = None
    cluster_name: Optional[str] = None
    # Full placement metadata (cluster_arn, subnetIds, cloudRegionId, vpcId, region…) from the locator JSONB
    locator: Optional[Dict[str, Any]] = None
    # Predefined reference-variable values derived from the locator (S3_BUCKET_NAME,
    # QUEUE_URL, AWS_REGION, …) — pre-fills the Add-variable picker.
    derived_variables: Optional[Dict[str, str]] = None
    # UI-ready deployment status (ResourceStatusEnum); "ONLINE" = deployed & active
    status: Optional[str] = None

    class Config:
        from_attributes = True


class InfrastructureListResponse(BaseModel):
    """Response schema for infrastructure list"""
    total: int
    infrastructures: List[InfrastructureListItem]


@router.get("/list", response_model=InfrastructureListResponse, summary="List Infrastructure Instances",
    access=AuthenticationOnly(reason="tenant-scoped listing; object-level cards pending typed routes"))
async def list_infrastructures(
    infrastructuretype_ref_code: Optional[str] = Query(None, description="Infrastructure type code (e.g., ecs_fargate_infrastructuretype_ref)"),
    environment: Optional[EnvironmentEnum] = Query(None, description="Environment (dev/staging/prod)"),
    geo_loc_mst_code: Optional[str] = Query(None, description="Geographic location code (optional filter)"),
    applications_mst_code: Optional[str] = Query(None, description="Application code (optional filter)"),
    cluster_name: Optional[str] = Query(None, description="Cluster name to look up directly from locator JSONB"),
    status: Optional[ResourceStatusEnum] = Query(None, description="Filter by status (e.g. ONLINE for deployed/available resources)"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    List infrastructure instances (clusters) filtered by infrastructure type, environment, and optionally geo location.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    This endpoint is used to populate the infrastructure dropdown in the service config form,
    allowing users to select which cluster/compute resource to deploy their service to.

    **Query Parameters:**
    - `infrastructuretype_ref_code`: Infrastructure type (required)
    - `environment`: Environment (required)
    - `geo_loc_mst_code`: Geographic location (optional)

    **Response:**
    ```json
    {
        "total": 2,
        "infrastructures": [
            {
                "code": "ecs-cluster-mumbai-prod",
                "name": "ECS Cluster Mumbai Production",
                "infrastructuretype_ref_code": "ecs_fargate_infrastructuretype_ref",
                "environment": "prod",
                "geo_loc_mst_code": "mumbai",
                "resource_identifier": "arn:aws:ecs:ap-south-1:123456789012:cluster/prod-cluster"
            }
        ]
    }
    ```

    **Use Case:**
    - Populate infrastructure dropdown in "Create Service Config" form
    - Filter available clusters by type and environment

    **Returns:**
    - `total`: Count of matching infrastructure instances
    - `infrastructures`: List of infrastructure items

    **Raises:**
    - `500`: Internal server error
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # Initialize service layer
        service = InfrastructureMstService(db)

        # Delegate to service layer
        result = await service.list_infrastructures(
            tenant_code=tenant.code,
            user_code=user.code,
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            applications_mst_code=applications_mst_code,
            cluster_name=cluster_name,
            status=status,
        )

        return result

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/get-detail", response_model=InfrastructureDetailResponse, summary="Get Infrastructure Detail by Code",
    access=AuthenticationOnly(reason="tenant-scoped detail; object-level card pending typed routes"))
async def get_infrastructure_detail(
    body: InfrastructureDetailRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Fetch a single infrastructure_mst record by code, including its locator JSONB.
    Used when clicking a canvas node to load its saved config.
    """
    try:
        user, tenant = user_and_tenant
        service = InfrastructureMstService(db)
        result = await service.get_detail(body.code, tenant.code)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Infrastructure '{body.code}' not found")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CloneSourceItem(BaseModel):
    """A single resource offered as a clone source."""
    code: str
    name: str
    infrastructuretype_ref_code: str
    environment: str
    geo_loc_mst_code: Optional[str] = None
    cluster_name: Optional[str] = None
    status: Optional[str] = None

    class Config:
        from_attributes = True


class CloneSourcesResponse(BaseModel):
    """One page of clone sources."""
    total: int
    skip: int
    limit: int
    has_more: bool
    infrastructures: List[CloneSourceItem]


@router.get("/clone-sources", response_model=CloneSourcesResponse, summary="Paginated Clone Sources",
    access=AuthenticationOnly(reason="tenant- and workspace-scoped listing; object-level cards pending typed routes"))
async def list_clone_sources(
    infrastructuretype_ref_code: str = Query(..., description="Infrastructure type to clone within"),
    exclude_code: Optional[str] = Query(None, description="Target resource code to exclude from the list"),
    environment: Optional[EnvironmentEnum] = Query(None, description="Environment filter"),
    geo_loc_mst_code: Optional[str] = Query(None, description="Geo location filter"),
    search: Optional[str] = Query(None, description="Case-insensitive match on the resource name"),
    skip: int = Query(0, ge=0, description="Rows to skip"),
    limit: int = Query(20, ge=1, le=100, description="Page size"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Eligible clone sources of the given type, filtered and paginated server-side.

    Backs the lazy-loading source list in the clone-settings modal. Scoped to the
    caller's tenant and to applications in workspaces they can access.

    Security:
        - JWT authentication required
        - Tenant isolation enforced
        - Workspace-scoped: only resources of accessible applications are returned
    """
    try:
        user, tenant = user_and_tenant
        service = InfrastructureMstService(db)
        return await service.list_clone_sources(
            tenant_code=tenant.code,
            user_code=user.code,
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            exclude_code=exclude_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            search=search.strip() if search else None,
            skip=skip,
            limit=limit,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/clone-settings", response_model=InfrastructureDetailResponse, summary="Clone Infra Settings Between Resources",
    access=AuthenticationOnly(reason="tenant-scoped settings copy; object-level card on the target pending typed routes"))
async def clone_infrastructure_settings(
    body: CloneInfraSettingsRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Copy the configuration values of one infrastructure resource into another
    of the same type (SQS, S3, DynamoDB, ...). The target keeps its own
    identifier and placement (region/account/cluster); only settings move.

    Returns the updated target detail including the merged locator.
    """
    try:
        user, tenant = user_and_tenant
        service = InfrastructureMstService(db)
        return await service.clone_settings(
            tenant_code=tenant.code,
            user_code=user.code,
            source_code=body.source_code,
            target_code=body.target_code,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class InfraNamePrefixResponse(BaseModel):
    """Response schema for infrastructure naming prefix + optional resolved name.

    `prefix` is the generic `{organization}-{env}-{region_code}-{index}` base
    used by DynamoDB/S3/EKS. `name` is the fully resolved AWS resource name —
    populated only when `infra_type` + `identifier` are provided, and it
    handles per-infra-type rules (e.g. SQS `{identifier}-{index}` with `.fifo`
    suffix for FIFO queues).
    """
    prefix: str
    name: Optional[str] = None


@router.get("/name-prefix", response_model=InfraNamePrefixResponse, summary="Get Infrastructure Naming Prefix",
    access=AuthenticationOnly(reason="returns a tenant naming string only; no object to check"))
async def get_infra_name_prefix(
    environment: str = Query("trial", description="Environment (e.g., trial, dev, prod)"),
    infra_type: Optional[str] = Query(None, description="infrastructuretype_ref_code — enables infra-type-aware `name` in response"),
    identifier: Optional[str] = Query(None, description="User-supplied identifier — required alongside infra_type to resolve `name`"),
    fifo_queue: bool = Query(False, description="SQS only — set true for FIFO queues"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """
    Returns the infrastructure resource naming prefix for the current tenant,
    and (when `infra_type` + `identifier` are supplied) the fully resolved AWS
    resource name.
    """
    _, tenant = user_and_tenant
    result = InfrastructureMstService.get_infra_naming(
        tenant_code=tenant.code,
        environment=environment,
        infra_type=infra_type,
        identifier=identifier,
        fifo_queue=fifo_queue,
    )
    return InfraNamePrefixResponse(**result)


@router.get("/status/{code}", response_model=InfrastructureStatusResponse, summary="Get Infrastructure Status",
    access=AuthenticationOnly(reason="tenant-scoped status; object-level card pending typed routes"))
async def get_infrastructure_status(
    code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get the deployment status of an infrastructure_mst entry by its code.
    """
    try:
        service = InfrastructureMstService(db)
        result = await service.get_status(code)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Infrastructure '{code}' not found")
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{code}", status_code=status.HTTP_200_OK, summary="Soft Delete Infrastructure",
    access=AuthenticationOnly(reason="tenant-scoped soft delete; object-level card pending typed routes"))
async def soft_delete_infrastructure(
    code: str,
    _: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Soft-delete an infrastructure_mst entry by setting its status to SOFT_DELETED.
    """
    repo = InfrastructureMstRepository(db)
    updated = await repo.update_status(code=code, status=ResourceStatusEnum.SOFT_DELETED)
    if updated == 0:
        raise HTTPException(status_code=404, detail=f"Infrastructure '{code}' not found")
    return {"code": code, "status": "soft_deleted"}


# ── Cluster inventory (EKS / ECS-EC2) ────────────────────────────────────────

class ClusterListItem(BaseModel):
    """One EKS/ECS-EC2 cluster row on the Clusters screen."""
    code: str
    name: str
    # Raw type code plus a short display label ("EKS" / "ECS EC2")
    infrastructuretype_ref_code: str
    cluster_type: str
    environment: str
    applications_mst_code: Optional[str] = None
    application_name: Optional[str] = None
    geo_loc_mst_code: Optional[str] = None
    geo_loc_name: Optional[str] = None
    # Cluster name and ARN read out of the locator JSONB
    cluster_name: Optional[str] = None
    cluster_arn: Optional[str] = None
    region: Optional[str] = None
    status: Optional[str] = None
    # locator.isRegistered — absent key reads as false
    is_registered: bool = False
    # locator.isListed — absent key reads as true
    is_listed: bool = True

    class Config:
        from_attributes = True


class ClusterListResponse(BaseModel):
    """Response schema for the cluster listing."""
    total: int
    clusters: List[ClusterListItem]


class ClusterFlagsRequest(BaseModel):
    """Toggle payload — omit a flag to leave it untouched."""
    is_registered: Optional[bool] = None
    is_listed: Optional[bool] = None


@router.get("/clusters", response_model=ClusterListResponse, summary="List EKS / ECS EC2 Clusters",
    access=AuthenticationOnly(reason="tenant- and workspace-scoped listing; object-level cards pending typed routes"))
async def list_clusters(
    infrastructuretype_ref_code: Optional[str] = Query(None, description="eks_infrastructuretype_ref or ecs_ec2_infrastructuretype_ref; omit for both"),
    environment: Optional[EnvironmentEnum] = Query(None, description="Environment filter (dev/staging/prod)"),
    geo_loc_mst_code: Optional[str] = Query(None, description="Geographic location filter"),
    search: Optional[str] = Query(None, description="Case-insensitive match on the cluster name"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    List every EKS and ECS-EC2 cluster for the tenant, including the ones that
    are not registered and the ones hidden from the canvas.

    Unlike `/list`, this does not filter on `locator.isRegistered` — the
    Clusters screen is where that flag is reviewed and toggled, so rows missing
    it must still appear (reported as `is_registered: false`).

    Security:
        - JWT authentication required
        - Tenant isolation enforced
        - Workspace-scoped: only clusters of accessible applications are returned

    **Response:**
    ```json
    {
        "total": 1,
        "clusters": [
            {
                "code": "eks-mumbai-prod",
                "name": "EKS Mumbai Production",
                "infrastructuretype_ref_code": "eks_infrastructuretype_ref",
                "cluster_type": "EKS",
                "environment": "prod",
                "applications_mst_code": "payments",
                "application_name": "Payments",
                "geo_loc_mst_code": "mumbai",
                "geo_loc_name": "Mumbai",
                "cluster_name": "prod-eks-1",
                "cluster_arn": "arn:aws:eks:ap-south-1:123456789012:cluster/prod-eks-1",
                "region": "ap-south-1",
                "status": "ONLINE",
                "is_registered": true,
                "is_listed": true
            }
        ]
    }
    ```

    **Raises:**
    - `400`: `infrastructuretype_ref_code` is not a cluster type
    - `500`: Internal server error
    """
    try:
        user, tenant = user_and_tenant
        service = InfrastructureMstService(db)
        return await service.list_clusters(
            tenant_code=tenant.code,
            user_code=user.code,
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            environment=environment,
            geo_loc_mst_code=geo_loc_mst_code,
            search=search.strip() if search else None,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/clusters/{code}/flags", response_model=ClusterListItem, summary="Toggle Cluster Registered / Listed Flags",
    access=AuthenticationOnly(reason="tenant- and workspace-scoped locator update; object-level card pending typed routes"))
async def update_cluster_flags(
    code: str,
    body: ClusterFlagsRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Set `locator.isRegistered` and/or `locator.isListed` on one cluster.

    Both flags are optional and independent — send only the one being toggled;
    the other keeps its current value. The rest of the locator is preserved
    (JSONB shallow merge).

    - `is_registered`: whether services may be created against this cluster
    - `is_listed`: whether the cluster (and its service nodes) show on the canvas

    **Raises:**
    - `400`: neither flag supplied, or the resource is not an EKS/ECS-EC2 cluster
    - `404`: cluster not found in the caller's tenant / accessible workspaces
    """
    try:
        user, tenant = user_and_tenant
        service = InfrastructureMstService(db)
        return await service.set_cluster_flags(
            tenant_code=tenant.code,
            user_code=user.code,
            code=code,
            is_registered=body.is_registered,
            is_listed=body.is_listed,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

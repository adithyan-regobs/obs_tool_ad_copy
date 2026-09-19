"""
Namespace Master API Endpoints

API routes for managing Kubernetes namespaces per infrastructure (EKS cluster).
"""
import uuid
from typing import Tuple
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.repository.namespace_mst_repository import NamespaceMstRepository
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.schemas.namespace_mst_schemas import (
    NamespaceMstResponse,
    NamespaceMstListResponse,
    NamespaceMstCreate,
    NamespaceDropdownResponse,
    NamespaceDropdownItem
)


router = APIRouter(prefix="/namespaces", tags=["Namespace Master"])


@router.get(
    "",
    response_model=NamespaceDropdownResponse,
    summary="Get Namespaces for Infrastructure",
    description="Get all active namespaces for a specific infrastructure (EKS cluster)"
)
async def get_namespaces(
    infrastructure_mst_code: str = Query(..., description="Infrastructure code (EKS cluster)"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> NamespaceDropdownResponse:
    """
    Get all namespaces for an infrastructure.

    Used for dropdown selection in EKS deployment configuration.
    """
    repo = NamespaceMstRepository(db)
    namespaces = await repo.list_by_infrastructure(infrastructure_mst_code)

    # Always include 'default' as the first option if not present
    namespace_items = [
        NamespaceDropdownItem(code=ns.code, namespace=ns.namespace)
        for ns in namespaces
    ]

    # Check if 'default' is in the list
    has_default = any(item.namespace == "default" for item in namespace_items)

    # If no namespaces exist or 'default' is not present, add it as a virtual option
    if not has_default:
        namespace_items.insert(0, NamespaceDropdownItem(
            code="default",
            namespace="default"
        ))

    return NamespaceDropdownResponse(namespaces=namespace_items)


@router.get(
    "/list",
    response_model=NamespaceMstListResponse,
    summary="List All Namespaces for Infrastructure",
    description="Get full list of namespaces with details for an infrastructure"
)
async def list_namespaces(
    infrastructure_mst_code: str = Query(..., description="Infrastructure code (EKS cluster)"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> NamespaceMstListResponse:
    """
    Get full list of namespaces with details.
    """
    repo = NamespaceMstRepository(db)
    namespaces = await repo.list_by_infrastructure(infrastructure_mst_code)

    return NamespaceMstListResponse(
        total=len(namespaces),
        namespaces=[
            NamespaceMstResponse(
                code=ns.code,
                name=ns.name,
                infrastructure_mst_code=ns.infrastructure_mst_code,
                namespace=ns.namespace,
                description=ns.description
            )
            for ns in namespaces
        ]
    )


@router.post(
    "",
    response_model=NamespaceMstResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Namespace",
    description="Create a new namespace for an infrastructure (EKS cluster)"
)
async def create_namespace(
    namespace_data: NamespaceMstCreate,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
) -> NamespaceMstResponse:
    """
    Create a new namespace for an infrastructure.

    The namespace must be unique per infrastructure.
    """
    user, tenant = user_and_tenant
    repo = NamespaceMstRepository(db)
    infra_repo = InfrastructureMstRepository(db)

    # Verify infrastructure exists
    infrastructure = await infra_repo.get_by_code(namespace_data.infrastructure_mst_code)
    if not infrastructure:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Infrastructure with code '{namespace_data.infrastructure_mst_code}' not found"
        )

    # Check for duplicate namespace
    existing = await repo.check_namespace_exists(
        namespace_data.infrastructure_mst_code,
        namespace_data.namespace
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Namespace '{namespace_data.namespace}' already exists for this infrastructure"
        )

    # Generate unique code
    code = f"ns_{namespace_data.namespace}_{str(uuid.uuid4())[:8]}"
    name = f"{namespace_data.namespace} namespace"

    namespace = await repo.create_namespace(
        code=code,
        name=name,
        infrastructure_mst_code=namespace_data.infrastructure_mst_code,
        namespace=namespace_data.namespace,
        description=namespace_data.description
    )

    await db.commit()

    return NamespaceMstResponse(
        code=namespace.code,
        name=namespace.name,
        infrastructure_mst_code=namespace.infrastructure_mst_code,
        namespace=namespace.namespace,
        description=namespace.description
    )


@router.delete(
    "/{code}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Namespace",
    description="Soft delete a namespace"
)
async def delete_namespace(
    code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Soft delete a namespace.

    Note: Cannot delete the 'default' namespace.
    """
    repo = NamespaceMstRepository(db)

    namespace = await repo.get_by_code(code)
    if not namespace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Namespace with code '{code}' not found"
        )

    # Prevent deletion of 'default' namespace
    if namespace.namespace == "default":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the 'default' namespace"
        )

    await repo.delete_namespace(code)
    await db.commit()

    return None

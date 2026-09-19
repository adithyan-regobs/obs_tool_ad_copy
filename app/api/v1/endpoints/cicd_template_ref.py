"""
API endpoints for CI/CD Template Reference operations.
Provides endpoints to manage CI/CD workflow template references.
"""
import uuid
from typing import Tuple, Optional, List
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, or_

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.cicd_template_ref_model import CicdTemplateRefModel
from app.schemas.cicd_template_ref_schemas import (
    CicdTemplateRefCreate,
    CicdTemplateRefUpdate,
    CicdTemplateRefResponse,
    CicdTemplateRefListResponse
)

router = APIRouter()


@router.get("", response_model=CicdTemplateRefListResponse, summary="List CI/CD Template References")
async def list_cicd_template_refs(
    service_config_code: Optional[str] = Query(None, description="Filter by service config code"),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    List all CI/CD template references for the current tenant.

    Security:
        - JWT authentication required

    **Query Parameters:**
    - `service_config_code`: (Optional) Filter templates for a specific service
    - `is_active`: (Optional) Filter by active status

    **Response:**
    ```json
    {
        "templates": [
            {
                "id": 1,
                "code": "cicd-ref-123",
                "name": "Production Workflow",
                "description": "Full CI/CD pipeline for production",
                "config": {
                    "steps": [...]
                },
                "is_active": true,
                "applications_mst_code": "svc-123",
                "tenant_mst_code": "tenant-001",
                "created_at": "2025-12-25T10:00:00Z",
                "updated_at": "2025-12-25T10:00:00Z"
            }
        ],
        "total": 1
    }
    ```
    """
    try:
        user, tenant = user_and_tenant

        # Build query - include both tenant-specific and global templates
        query = select(CicdTemplateRefModel).where(
            or_(
                CicdTemplateRefModel.tenant_mst_code == tenant.code,
                CicdTemplateRefModel.tenant_mst_code.is_(None)
            )
        )

        # Apply filters
        if service_config_code:
            query = query.where(CicdTemplateRefModel.applications_mst_code == service_config_code)

        if is_active is not None:
            query = query.where(CicdTemplateRefModel.is_active == is_active)

        # Execute query
        result = await db.execute(query.order_by(CicdTemplateRefModel.created_at.desc()))
        templates = result.scalars().all()

        return CicdTemplateRefListResponse(
            templates=[CicdTemplateRefResponse.model_validate(tmpl) for tmpl in templates],
            total=len(templates)
        )

    except Exception as e:
        import traceback
        print(f"Error fetching template references: {str(e)}")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Failed to fetch template references: {str(e)}")


@router.get("/{template_code}", response_model=CicdTemplateRefResponse, summary="Get CI/CD Template Reference by Code")
async def get_cicd_template_ref(
    template_code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get a specific CI/CD template reference by code.

    Security:
        - JWT authentication required

    **Path Parameters:**
    - `template_code`: Unique template code

    **Response:**
    Returns the template reference details including configuration and steps.
    """
    try:
        user, tenant = user_and_tenant

        # Fetch template
        query = select(CicdTemplateRefModel).where(
            CicdTemplateRefModel.code == template_code,
            or_(
                CicdTemplateRefModel.tenant_mst_code == tenant.code,
                CicdTemplateRefModel.tenant_mst_code.is_(None)
            )
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"Template reference not found: {template_code}"
            )

        return CicdTemplateRefResponse.model_validate(template)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch template reference: {str(e)}")


@router.post("", response_model=CicdTemplateRefResponse, summary="Create CI/CD Template Reference")
async def create_cicd_template_ref(
    template_data: CicdTemplateRefCreate,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new CI/CD template reference.

    Security:
        - JWT authentication required

    **Request Body:**
    ```json
    {
        "name": "Production Workflow",
        "description": "Full CI/CD pipeline for production",
        "config": {
            "steps": [
                {
                    "id": "code-checkout",
                    "name": "Code Checkout",
                    "order": 1,
                    "mandatory": true,
                    "enabled": true,
                    "category": "setup",
                    "description": "Checkout source code from repository"
                }
            ]
        },
        "applications_mst_code": "svc-123",
        "is_active": true
    }
    ```

    **Response:**
    Returns the created template reference with generated code and timestamps.
    """
    try:
        user, tenant = user_and_tenant

        # Generate unique code using UUID
        code = f"cicd-ref-{str(uuid.uuid4())[:8]}"

        # Create template instance
        template = CicdTemplateRefModel(
            code=code,
            tenant_mst_code=tenant.code,
            name=template_data.name,
            description=template_data.description,
            config=template_data.config.model_dump() if template_data.config else None,
            is_active=template_data.is_active,
            applications_mst_code=template_data.applications_mst_code
        )

        db.add(template)
        await db.commit()
        await db.refresh(template)

        return CicdTemplateRefResponse.model_validate(template)

    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create template reference: {str(e)}")


@router.put("/{template_code}", response_model=CicdTemplateRefResponse, summary="Update CI/CD Template Reference")
async def update_cicd_template_ref(
    template_code: str,
    template_data: CicdTemplateRefUpdate,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Update an existing CI/CD template reference.

    Security:
        - JWT authentication required

    **Path Parameters:**
    - `template_code`: Unique template code

    **Request Body:**
    All fields are optional. Only provided fields will be updated.

    **Response:**
    Returns the updated template reference.
    """
    try:
        user, tenant = user_and_tenant

        # Fetch template
        query = select(CicdTemplateRefModel).where(
            CicdTemplateRefModel.code == template_code,
            CicdTemplateRefModel.tenant_mst_code == tenant.code
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"Template reference not found: {template_code}"
            )

        # Update fields
        if template_data.name is not None:
            template.name = template_data.name
        if template_data.description is not None:
            template.description = template_data.description
        if template_data.config is not None:
            template.config = template_data.config.model_dump()
        if template_data.is_active is not None:
            template.is_active = template_data.is_active
        if template_data.applications_mst_code is not None:
            template.applications_mst_code = template_data.applications_mst_code

        await db.commit()
        await db.refresh(template)

        return CicdTemplateRefResponse.model_validate(template)

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to update template reference: {str(e)}")


@router.delete("/{template_code}", summary="Delete CI/CD Template Reference")
async def delete_cicd_template_ref(
    template_code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Delete a CI/CD template reference.

    Security:
        - JWT authentication required

    **Path Parameters:**
    - `template_code`: Unique template code

    **Response:**
    ```json
    {
        "message": "Template reference deleted successfully"
    }
    ```
    """
    try:
        user, tenant = user_and_tenant

        # Fetch template
        query = select(CicdTemplateRefModel).where(
            CicdTemplateRefModel.code == template_code,
            CicdTemplateRefModel.tenant_mst_code == tenant.code
        )
        result = await db.execute(query)
        template = result.scalar_one_or_none()

        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"Template reference not found: {template_code}"
            )

        await db.delete(template)
        await db.commit()

        return {"message": "Template reference deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete template reference: {str(e)}")

"""
Audit Trail API Endpoints

Provides audit trail viewing for organization owners.
"""
import logging
from typing import Tuple, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, require_organization_owner
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.audit_trail_service import AuditTrailService
from app.schemas.audit_trail_schemas import (
    AuditTrailListResponse,
    AuditEventDetailResponse,
)
from app.core.enum import AuditActionEnum

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("", response_model=AuditTrailListResponse)
async def get_audit_trail(
    page: int = Query(1, ge=1, description="Page number (starts at 1)"),
    page_size: int = Query(50, ge=1, le=100, description="Number of events per page (max 100)"),
    event_name: Optional[AuditActionEnum] = Query(None, description="Filter by event type (CREATE, UPDATE, DELETE, READ)"),
    date_from: Optional[datetime] = Query(None, description="Filter events after this date (inclusive)"),
    date_to: Optional[datetime] = Query(None, description="Filter events before this date (inclusive)"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(require_organization_owner),
    session: AsyncSession = Depends(get_db),
):
    """
    Get paginated audit trail for the current organization.

    **Authorization:** Only organization owners (isOrganizationOwner=true)

    **Tenant Isolation:** Automatically filtered to user's organization

    **Filters:**
    - event_name: CREATE, UPDATE, DELETE, READ
    - date_from: Events after this date
    - date_to: Events before this date

    **Returns:**
    - Paginated list of audit events with actor info and field changes
    """
    user, tenant = user_and_tenant

    logger.info(
        f"[AUDIT TRAIL] Fetching events for tenant {tenant.code}, "
        f"page={page}, page_size={page_size}, event_name={event_name}"
    )

    try:
        # Use service layer for business logic
        service = AuditTrailService(session)
        return await service.get_audit_trail_paginated(
            tenant_code=tenant.code,
            page=page,
            page_size=page_size,
            event_name=event_name,
            date_from=date_from,
            date_to=date_to,
        )

    except Exception as e:
        logger.error(f"Failed to fetch audit trail: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch audit trail. Please try again later."
        )


@router.get("/{event_id}", response_model=AuditEventDetailResponse)
async def get_audit_event_detail(
    event_id: int,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(require_organization_owner),
    session: AsyncSession = Depends(get_db),
):
    """
    Get detailed view of a single audit event.

    **Authorization:** Only organization owners

    **Tenant Isolation:** Event must belong to user's organization

    **Returns:**
    - Full event details including request/response payloads
    """
    user, tenant = user_and_tenant

    logger.info(f"[AUDIT TRAIL] Fetching event {event_id} for tenant {tenant.code}")

    try:
        # Use service layer for business logic
        service = AuditTrailService(session)
        event_detail = await service.get_audit_event_detail(
            event_id=event_id,
            tenant_code=tenant.code
        )

        if not event_detail:
            logger.warning(
                f"Event {event_id} not found or doesn't belong to tenant {tenant.code}"
            )
            raise HTTPException(
                status_code=404,
                detail=f"Audit event {event_id} not found or access denied"
            )

        return event_detail

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch event detail: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to fetch event details. Please try again later."
        )

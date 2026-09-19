"""
API endpoints for ticket management
"""
import logging
from typing import Tuple
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ticket_schemas import (
    GenerateTicketNumberRequest,
    GenerateTicketNumberResponse,
    TicketResponse,
    TicketHistoryResponse
)
from app.services.ticket_service import TicketService
from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/generate-ticket-number", response_model=GenerateTicketNumberResponse)
async def generate_ticket_number(
    request: GenerateTicketNumberRequest,
    user_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Generate a new ticket with sequential ticket number.

    This endpoint is called when user clicks "New Ticket" button.
    It generates a globally unique sequential ticket number (TICKET-001, TICKET-002, etc.)
    and stores the ticket in the database.

    **Concurrency-safe:** Multiple simultaneous requests will each get unique ticket numbers.
    Uses retry logic with unique constraint to handle race conditions.

    **Architecture Flow:**
    - Endpoint receives request
    - JWT provides tenant and user codes (automatically extracted)
    - Service layer handles business logic and retry mechanism
    - Repository layer performs database operations
    - Unique constraint on ticket_number prevents duplicates

    Args:
        request: Ticket creation details (name, description)
        user_tenant: Current authenticated user and tenant (from JWT)
        db: Database session

    Returns:
        GenerateTicketNumberResponse with ticket number and details

    Raises:
        HTTPException 400: Invalid tenant or user code
        HTTPException 500: Failed to generate ticket after retries
    """
    current_user, current_tenant = user_tenant

    # Initialize service
    ticket_service = TicketService(db)

    # Generate ticket (service handles all business logic including retries)
    # Note: tenant and user codes are extracted from JWT, not from request body
    ticket = await ticket_service.generate_ticket_number(
        tenants_mst_code=current_tenant.code,
        user_mst_code=current_user.code,
        name=request.name,
        description=request.description
    )
    
    logger.info(
        f"User {current_user.code} generated ticket {ticket.ticket_number} "
        f"for tenant {current_tenant.code}"
    )
    
    return GenerateTicketNumberResponse(
        success=True,
        message="Ticket created successfully",
        ticket_number=ticket.ticket_number,
        ticket_code=ticket.code,
        id=ticket.id,
        created_at=ticket.created_at
    )

@router.get("/history", response_model=TicketHistoryResponse)
async def get_ticket_history(
    limit: int = 50,
    offset: int = 0,
    user_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get ticket history for current tenant.

    Args:
        limit: Number of tickets to return (default: 50)
        offset: Pagination offset (default: 0)
        user_tenant: Current authenticated user and tenant
        db: Database session

    Returns:
        TicketHistoryResponse with tickets list and total count
    """
    current_user, current_tenant = user_tenant

    # Initialize service
    ticket_service = TicketService(db)

    # Get ticket history
    tickets, total = await ticket_service.get_ticket_history(
        tenant_code=current_tenant.code,
        user_mst_code=current_user.code,
        limit=limit,
        offset=offset
    )

    logger.info(
        f"User {current_user.code} retrieved ticket history for tenant {current_tenant.code}: "
        f"{len(tickets)} tickets (total: {total})"
    )

    return TicketHistoryResponse(
        tickets=[TicketResponse.from_orm(t) for t in tickets],
        total=total
    )

@router.get("/{ticket_number}", response_model=TicketResponse)
async def get_ticket_by_number(
    ticket_number: str,
    user_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get ticket details by ticket number.
    
    Args:
        ticket_number: Ticket number (e.g., "TICKET-001")
        user_tenant: Current authenticated user and tenant
        db: Database session
        
    Returns:
        Full ticket details
        
    Raises:
        HTTPException 404: Ticket not found
    """
    ticket_service = TicketService(db)
    ticket = await ticket_service.get_ticket_by_number(ticket_number)

    return TicketResponse.from_orm(ticket)




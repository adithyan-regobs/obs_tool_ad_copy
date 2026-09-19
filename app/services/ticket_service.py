"""
Ticket Service
Business logic for ticket operations including sequential number generation
"""
import logging
from typing import Dict, Any, Tuple, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, func
from fastapi import HTTPException, status

from app.repository.ticket_repository import TicketRepository
from app.db.models.ticket_model import TicketModel

logger = logging.getLogger(__name__)


class TicketService:
    """Service for handling ticket operations"""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.ticket_repo = TicketRepository(db)

    async def generate_ticket_number(
        self,
        tenants_mst_code: str,
        user_mst_code: str,
        name: str,
        description: str = None,
        source: str = None,
        source_ref_id: str = None,
        max_retries: int = 5
    ) -> TicketModel:
        """
        Generate a new ticket with sequential ticket number.
        
        Handles concurrency by retrying on duplicate ticket_number violations.
        The retry logic handles race conditions when multiple users click "New Ticket"
        simultaneously.
        
        Flow:
        1. Get next ticket number from MAX(ticket_number) + 1
        2. Try to create ticket with this number
        3. If duplicate detected (race condition), retry with new number
        4. Repeat up to max_retries times
        
        Args:
            tenants_mst_code: Tenant code
            user_mst_code: User code (assigned user)
            name: Ticket name/title
            description: Optional ticket description
            max_retries: Maximum retry attempts for concurrency conflicts
            
        Returns:
            Created TicketModel instance
            
        Raises:
            HTTPException: If unable to create ticket after max retries
        """
        for attempt in range(max_retries):
            try:
                # Get next ticket number
                next_num = await self.ticket_repo.get_next_ticket_number()
                ticket_number = f"TICKET-{next_num:03d}"
                
                logger.info(
                    f"Attempt {attempt + 1}: Generating ticket {ticket_number} "
                    f"for tenant {tenants_mst_code}, user {user_mst_code}"
                )
                
                # Try to create ticket with this number
                ticket = await self.ticket_repo.create_with_ticket_number(
                    ticket_number=ticket_number,
                    tenants_mst_code=tenants_mst_code,
                    user_mst_code=user_mst_code,
                    name=name,
                    description=description,
                    source=source,
                    source_ref_id=source_ref_id
                )
                
                logger.info(
                    f"Successfully created ticket {ticket_number} "
                    f"(code: {ticket.code}, id: {ticket.id})"
                )
                
                return ticket
                
            except IntegrityError as e:
                await self.db.rollback()
                
                # Check if it's a duplicate ticket_number error
                error_str = str(e.orig).lower()
                if "ticket_number" in error_str or "uq_ticket_ticket_number" in error_str:
                    logger.warning(
                        f"Duplicate ticket number detected on attempt {attempt + 1}. "
                        f"Another user created this ticket concurrently. Retrying..."
                    )
                    
                    if attempt < max_retries - 1:
                        # Retry with next number
                        continue
                    else:
                        # Max retries exceeded
                        logger.error(
                            f"Failed to create ticket after {max_retries} attempts "
                            f"due to concurrent requests"
                        )
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to generate ticket number due to high concurrency. Please try again."
                        )
                else:
                    # Different integrity error (e.g., invalid FK, invalid tenant/user)
                    logger.error(f"Integrity error creating ticket: {str(e)}")
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invalid tenant or user code provided"
                    )
                    
            except Exception as e:
                await self.db.rollback()
                logger.error(f"Unexpected error creating ticket: {str(e)}", exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to create ticket: {str(e)}"
                )

        # This shouldn't be reached, but just in case
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create ticket after multiple attempts"
        )

    async def get_ticket_by_number(self, ticket_number: str) -> TicketModel:
        """
        Get ticket by ticket number.
        
        Args:
            ticket_number: Ticket number (e.g., "TICKET-001")
            
        Returns:
            TicketModel instance
            
        Raises:
            HTTPException: If ticket not found
        """
        ticket = await self.ticket_repo.get_by_ticket_number(ticket_number)
        
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ticket {ticket_number} not found"
            )
        
        return ticket

    async def get_ticket_by_code(self, code: str) -> TicketModel:
        """
        Get ticket by code.
        
        Args:
            code: Ticket code (e.g., "TKT-ABC12345")
            
        Returns:
            TicketModel instance
            
        Raises:
            HTTPException: If ticket not found
        """
        ticket = await self.ticket_repo.get_by_code(code)
        
        if not ticket:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ticket with code {code} not found"
            )

        return ticket

    async def get_ticket_history(
        self,
        tenant_code: str,
        user_mst_code: str,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[List[TicketModel], int]:
        """
        Get ticket history for a specific user in a tenant.

        Args:
            tenant_code: Tenant code to filter tickets
            user_mst_code: User code to filter tickets
            limit: Max number of tickets to return
            offset: Pagination offset

        Returns:
            Tuple of (tickets list with ticket_number field, total count)
        """
        # Get total count
        count_query = select(func.count()).select_from(TicketModel).where(
            TicketModel.tenants_mst_code == tenant_code,
            TicketModel.user_mst_code == user_mst_code,
            TicketModel.is_deleted == False
        )
        total_result = await self.db.execute(count_query)
        total = total_result.scalar()

        # Get tickets with pagination (ordered by newest first)
        query = select(TicketModel).where(
            TicketModel.tenants_mst_code == tenant_code,
            TicketModel.user_mst_code == user_mst_code,
            TicketModel.is_deleted == False
        ).order_by(
            TicketModel.created_at.desc()
        ).limit(limit).offset(offset)

        result = await self.db.execute(query)
        tickets = result.scalars().all()

        return tickets, total

    async def get_ticket_by_source_ref(
        self,
        source: str,
        source_ref_id: str
    ) -> TicketModel:
        """Get ticket by source and source_ref_id.

        Args:
            source: Source system (e.g., "slack")
            source_ref_id: Reference ID from source system

        Returns:
            TicketModel instance or None if not found
        """
        query = select(TicketModel).where(
            TicketModel.source == source,
            TicketModel.source_ref_id == source_ref_id,
            TicketModel.is_deleted == False
        )

        result = await self.db.execute(query)
        return result.scalar_one_or_none()

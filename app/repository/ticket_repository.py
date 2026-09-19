"""
Ticket Repository
Handles database operations for tickets including sequential number generation
"""
import uuid
import logging
from typing import Optional
from sqlalchemy import select, func, cast, Integer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.db.models.ticket_model import TicketModel
from app.repository.base_repository import BaseRepository

logger = logging.getLogger(__name__)


class TicketRepository(BaseRepository[TicketModel]):
    """Repository for ticket operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(TicketModel, session)

    async def get_next_ticket_number(self) -> int:
        """
        Get the next sequential ticket number by finding the max numeric portion.

        Uses CAST to extract the numeric part after 'TICKET-' so that
        MAX works numerically, not lexicographically on the string column.

        Returns:
            Next ticket number as integer (1 if no tickets exist)
        """
        # Extract numeric suffix and find the true numeric max
        numeric_part = cast(
            func.substr(TicketModel.ticket_number, 8),  # len('TICKET-') + 1 = 8
            Integer
        )
        stmt = select(func.max(numeric_part))
        result = await self.session.execute(stmt)
        max_num = result.scalar()

        if max_num is not None:
            return max_num + 1
        else:
            return 1

    async def create_with_ticket_number(
        self,
        ticket_number: str,
        tenants_mst_code: str,
        user_mst_code: str,
        name: str,
        description: Optional[str] = None,
        source: Optional[str] = None,
        source_ref_id: Optional[str] = None
    ) -> TicketModel:
        """
        Create a new ticket with the provided ticket number.
        
        Args:
            ticket_number: Pre-generated ticket number (e.g., "TICKET-001")
            tenants_mst_code: Tenant code
            user_mst_code: User code (assigned user)
            name: Ticket name/title
            description: Optional ticket description
            
        Returns:
            Created TicketModel instance
            
        Raises:
            IntegrityError: If ticket_number already exists (handled by service layer)
        """
        # Generate unique code for BaseModel
        code = f"TKT-{uuid.uuid4().hex[:8].upper()}"
        
        # Create ticket
        ticket = TicketModel(
            code=code,
            name=name,
            description=description,
            tenants_mst_code=tenants_mst_code,
            user_mst_code=user_mst_code,
            ticket_number=ticket_number,
            source=source,
            source_ref_id=source_ref_id,
            is_active=True,
            is_deleted=False
        )
        
        self.session.add(ticket)
        await self.session.flush()
        await self.session.refresh(ticket)
        
        return ticket

    async def get_by_ticket_number(self, ticket_number: str) -> Optional[TicketModel]:
        """Get ticket by ticket number"""
        stmt = select(TicketModel).where(TicketModel.ticket_number == ticket_number)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Optional[TicketModel]:
        """Get ticket by code"""
        return await self.get_by(code=code)

"""
Base repository pattern for data access layer
"""
from typing import TypeVar, Generic, Type, Optional, List, Any, Dict
from uuid import UUID
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.base_model import Base

ModelType = TypeVar("ModelType", bound=Base)

class BaseRepository(Generic[ModelType]):
    """Base repository with common CRUD operations"""
    def __init__(self, model: Type[ModelType], session: AsyncSession):
        """Initialize repository with model and session"""
        self.model = model
        self.session = session

    async def get(self, id: UUID) -> Optional[ModelType]:
        """Get single record by ID"""
        stmt = select(self.model).where(self.model.id == id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, id: int) -> Optional[ModelType]:
        """Get single record by integer ID"""
        stmt = select(self.model).where(self.model.id == id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by(self, **kwargs) -> Optional[ModelType]:
        """Get single record by field values"""
        stmt = select(self.model).filter_by(**kwargs)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_multi(
        self,
        skip: int = 0,
        limit: int = 100,
        filters: Optional[List[Any]] = None,
        order_by: Optional[Any] = None
    ) -> List[ModelType]:
        """Get multiple records with pagination"""
        stmt = select(self.model)
        if filters:
            stmt = stmt.where(and_(*filters))
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = stmt.offset(skip).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_all(
        self,
        filters: Optional[List[Any]] = None,
        order_by: Optional[Any] = None,
    ) -> List[ModelType]:
        """Return all records (no pagination). Use cautiously on large tables."""
        stmt = select(self.model)
        if filters:
            stmt = stmt.where(and_(*filters))
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        result = await self.session.execute(stmt)
        result=result.scalars().all()
        result_list = [m.__dict__ for m in result]
        for item in result_list:
            item.pop("_sa_instance_state", None)
        return result_list


    async def count(self, filters: Optional[List[Any]] = None) -> int:
        """Count records with optional filters"""
        stmt = select(func.count()).select_from(self.model)
        if filters:
            stmt = stmt.where(and_(*filters))
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def create(self, **kwargs) -> ModelType:
        """Create new record"""
        db_obj = self.model(**kwargs)
        self.session.add(db_obj)
        await self.session.flush()
        await self.session.refresh(db_obj)
        return db_obj

    async def update(
        self,
        db_obj: ModelType,
        updates: Dict[str, Any]
    ) -> ModelType:
        """Update existing record"""
        for field, value in updates.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)
        self.session.add(db_obj)
        await self.session.flush()
        await self.session.refresh(db_obj)
        return db_obj

    async def delete(self, id: UUID) -> bool:
        """Hard delete record"""
        db_obj = await self.get(id)
        if db_obj:
            await self.session.delete(db_obj)
            await self.session.flush()
            return True
        return False

    async def soft_delete(self, id: UUID) -> Optional[ModelType]:
        """Soft delete record if model has SoftDeleteMixin"""
        db_obj = await self.get(id)
        if db_obj and hasattr(db_obj, 'soft_delete'):
            db_obj.soft_delete()
            self.session.add(db_obj)
            await self.session.flush()
            await self.session.refresh(db_obj)
            return db_obj
        return None

    async def exists(self, **kwargs) -> bool:
        """Check if record exists"""
        stmt = select(func.count()).select_from(self.model).filter_by(**kwargs)
        result = await self.session.execute(stmt)
        count = result.scalar() or 0
        return count > 0
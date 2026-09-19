from typing import List, Optional
from sqlalchemy import bindparam, select, update
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.variable_mst_model import VariableMstModel
from app.repository.base_repository import BaseRepository


class VariableMstRepository(BaseRepository[VariableMstModel]):

    def __init__(self, session: AsyncSession):
        super().__init__(VariableMstModel, session)

    async def get_by_owner(
        self,
        table_name: str,
        transaction_code: str,
    ) -> List[VariableMstModel]:
        """Get all variables for a polymorphic owner (e.g. SERVICE_CONFIG + code)."""
        stmt = (
            select(self.model)
            .options(joinedload(self.model.referenced_variable))
            .where(
                self.model.table_name == table_name,
                self.model.transaction_code == transaction_code,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_by_owner_and_env(
        self,
        table_name: str,
        transaction_code: str,
        environment: str,
    ) -> List[VariableMstModel]:
        """Get variables for an owner scoped to a specific environment."""
        stmt = (
            select(self.model)
            .options(joinedload(self.model.referenced_variable))
            .where(
                self.model.table_name == table_name,
                self.model.transaction_code == transaction_code,
                self.model.environments_enum == environment,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_by_key(
        self,
        table_name: str,
        transaction_code: str,
        key: str,
    ) -> Optional[VariableMstModel]:
        """Find a specific variable by owner + key name."""
        stmt = (
            select(self.model)
            .options(joinedload(self.model.referenced_variable))
            .where(
                self.model.table_name == table_name,
                self.model.transaction_code == transaction_code,
                self.model.key == key,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_key_and_env(
        self,
        table_name: str,
        transaction_code: str,
        key: str,
        environment: str,
    ) -> Optional[VariableMstModel]:
        """Find a specific variable by owner + key name + environment."""
        stmt = (
            select(self.model)
            .options(joinedload(self.model.referenced_variable))
            .where(
                self.model.table_name == table_name,
                self.model.transaction_code == transaction_code,
                self.model.key == key,
                self.model.environments_enum == environment,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_ids(self, ids: List[int]) -> List[VariableMstModel]:
        """Get variables by a list of IDs (for resolving variable_ids arrays)."""
        if not ids:
            return []
        stmt = (
            select(self.model)
            .options(joinedload(self.model.referenced_variable))
            .where(
                self.model.id.in_(ids),
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_by_codes(self, codes: List[str]) -> List[VariableMstModel]:
        """Batch-fetch variables by their unique code column in one query.

        Replaces N per-item get_by(code=...) lookups during deploy.
        """
        if not codes:
            return []
        stmt = (
            select(self.model)
            .where(
                self.model.code.in_(codes),
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_by_tenant(
        self,
        tenant_code: str,
    ) -> List[VariableMstModel]:
        """Get all variables for a tenant."""
        stmt = (
            select(self.model)
            .options(joinedload(self.model.referenced_variable))
            .where(
                self.model.tenants_mst_code == tenant_code,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_global_variables(
        self,
        tenant_code: str,
    ) -> List[VariableMstModel]:
        """Get all GLOBAL-scoped variables for a tenant."""
        stmt = (
            select(self.model)
            .where(
                self.model.scope_type == "GLOBAL",
                self.model.tenants_mst_code == tenant_code,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_referencing_variables(
        self,
        variable_id: int,
    ) -> List[VariableMstModel]:
        """Get all variables that reference a given variable (reverse lookup)."""
        stmt = (
            select(self.model)
            .where(
                self.model.referenced_variable_id == variable_id,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ── Canvas variable methods (used by ProjectVariablesService) ─────────

    async def get_by_transaction_and_key(
        self,
        table_name,
        transaction_code: str,
        key: str,
        environment,
    ) -> Optional[VariableMstModel]:
        """Find a variable by polymorphic owner + key + environment.
        Used for duplicate checking before creating a new canvas variable."""
        stmt = (
            select(self.model)
            .where(
                self.model.table_name == table_name,
                self.model.transaction_code == transaction_code,
                self.model.key == key,
                self.model.environments_enum == environment,
                self.model.is_deleted == False,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_by_transaction(
        self,
        table_name,
        transaction_code: str,
        environment,
    ) -> List[VariableMstModel]:
        """Get all variables for a polymorphic owner scoped to an environment."""
        stmt = (
            select(self.model)
            .options(joinedload(self.model.referenced_variable))
            .where(
                self.model.table_name == table_name,
                self.model.transaction_code == transaction_code,
                self.model.environments_enum == environment,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def get_existing_by_keys(
        self,
        table_name,
        transaction_code: str,
        environment,
        keys: List[str],
    ) -> dict:
        """Non-deleted rows for the given keys, mapped {key: row}.

        One query for the whole key set — replaces a per-key lookup so a bulk
        upsert (e.g. clone) does a single round-trip instead of N.
        """
        if not keys:
            return {}
        stmt = select(self.model).where(
            self.model.table_name == table_name,
            self.model.transaction_code == transaction_code,
            self.model.environments_enum == environment,
            self.model.key.in_(keys),
            self.model.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        return {row.key: row for row in result.scalars().all()}

    async def bulk_add(self, rows: List[VariableMstModel], chunk_size: int = 200) -> None:
        """Add many new rows and flush in chunks (pipelined INSERT batches).

        No per-row refresh: callers assign the business key (code) themselves,
        so nothing needs to be read back. The same flush also persists in-place
        updates to already-managed rows in this session.

        Chunked to bound each INSERT well under Postgres' 65535 bind-parameter
        limit for very large batches (SQLAlchemy also auto-batches, so this is a
        safety net rather than a strict need at typical sizes).
        """
        if not rows:
            await self.session.flush()
            return
        for start in range(0, len(rows), chunk_size):
            self.session.add_all(rows[start:start + chunk_size])
            await self.session.flush()

    async def get_by_cloud_identifier(
        self,
        identifier: str,
    ) -> Optional[VariableMstModel]:
        """Find a variable by variable_cloud_identifier. Used for ref duplicate checks."""
        stmt = (
            select(self.model)
            .where(
                self.model.variable_cloud_identifier == identifier,
                self.model.is_deleted == False,
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_by_application_and_environment(
        self,
        application_code: str,
        environment,
        tenant_code: str,
    ) -> List[VariableMstModel]:
        """Bulk load all canvas variables for an application + environment.
        Filters on application_code stored in metadata_json JSONB column."""
        stmt = (
            select(self.model)
            .options(joinedload(self.model.referenced_variable))
            .where(
                self.model.metadata_json["application_code"].astext == application_code,
                self.model.environments_enum == environment,
                self.model.tenants_mst_code == tenant_code,
                self.model.is_deleted == False,
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().unique().all())

    async def bulk_update_by_code(self, updates: List[dict]) -> None:
        """Update multiple rows by their unique code column.

        Each dict must have 'code' (used as WHERE filter) plus the column values
        to set. Resolves code→id in one SELECT then runs one executemany UPDATE.
        """
        if not updates:
            return
        codes = [u["code"] for u in updates]
        rows = await self.session.execute(
            select(self.model.id, self.model.code).where(self.model.code.in_(codes))
        )
        code_to_id = {row.code: row.id for row in rows}
        params = [
            {"id": code_to_id[u["code"]], **{k: v for k, v in u.items() if k != "code"}}
            for u in updates
            if u["code"] in code_to_id
        ]
        if params:
            await self.session.execute(
                update(self.model).execution_options(synchronize_session=False),
                params,
            )
        await self.session.flush()

    async def soft_delete(self, record_id: int) -> None:
        """Soft-delete a variable record by ID."""
        stmt = select(self.model).where(self.model.id == record_id)
        result = await self.session.execute(stmt)
        record = result.scalar_one_or_none()
        if record:
            record.is_deleted = True
            record.is_active = False
            await self.session.flush()

    async def soft_delete_by_ids(self, ids: List[int]) -> int:
        """Soft-delete multiple variable records in a single UPDATE.

        Returns the number of rows updated. Skips already-deleted rows.
        """
        if not ids:
            return 0
        stmt = (
            update(self.model)
            .where(
                self.model.id.in_(ids),
                self.model.is_deleted == False,
            )
            .values(is_deleted=True, is_active=False)
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount or 0

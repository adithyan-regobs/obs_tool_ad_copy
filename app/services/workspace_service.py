"""
Workspace service: orchestrates workspace creation, user-mapping
operations, and user-scoped access checks on top of repositories.
Business rules and tenant isolation live here.
"""
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enum import WorkspaceRoleEnum, WorkspaceStatusEnum
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.user_mst_model import UserMstModel
from app.db.models.workspace_user_map_model import WorkspaceUserMapModel
from app.domain.factories.workspace_mst_factory import (
    make_workspace,
    make_workspace_user_mapping,
)
from app.domain.validators.workspace_rules import (
    WorkspaceValidationError,
    WorkspaceValidator,
)
from app.repository.user_mst_repository import UserMstRepository
from app.repository.workspace_mst_repository import WorkspaceMstRepository
from app.repository.workspace_user_mapping_repository import (
    WorkspaceUserMappingRepository,
)
from app.schemas.workspace_schemas import (
    AddUsersToWorkspaceRequest,
    CreateWorkspaceRequest,
)

logger = logging.getLogger(__name__)


class WorkspaceServiceError(Exception):
    """Generic service-layer error with an associated HTTP status code."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class WorkspaceService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.workspace_repo = WorkspaceMstRepository(session)
        self.mapping_repo = WorkspaceUserMappingRepository(session)
        self.user_repo = UserMstRepository(session)

    # Backwards-compatible alias used by existing callers.
    @property
    def workspace_repository(self) -> WorkspaceMstRepository:
        return self.workspace_repo

    # ─── create ────────────────────────────────────────────────────────────
    async def create_workspace(
        self,
        user: UserMstModel,
        tenant: TenantsMstModel,
        data: CreateWorkspaceRequest,
    ) -> Dict[str, Any]:
        """
        Create a workspace under the caller's tenant and add the creator
        to workspace_user_mapping with role=owner. Both rows are written
        in the same session — `get_db` commits on success and rolls back
        on exception, so the operation is atomic.
        """
        WorkspaceValidator.validate_create_request(
            tenant_code=tenant.code,
            workspace_name=data.workspace_name,
            description=data.description,
        )

        existing = await self.workspace_repo.get_by_name_and_tenant(
            name=data.workspace_name,
            tenant_code=tenant.code,
        )
        if existing is not None:
            raise WorkspaceServiceError(
                status_code=409,
                detail=(
                    f"A workspace named '{data.workspace_name}' already exists "
                    f"in this organization"
                ),
            )

        workspace_data = make_workspace(
            tenant_code=tenant.code,
            workspace_name=data.workspace_name,
            description=data.description,
        )
        created_workspace = await self.workspace_repo.create(**workspace_data)
        logger.info(
            f"Workspace created code={created_workspace.code} tenant={tenant.code}"
        )

        mapping_data = make_workspace_user_mapping(
            workspace_code=created_workspace.code,
            workspace_name=created_workspace.name,
            user_mst_code=user.code,
            user_email=user.email_id,
            tenant_code=tenant.code,
            role=WorkspaceRoleEnum.owner,
        )
        await self.mapping_repo.create(**mapping_data)
        logger.info(
            f"Owner mapping created workspace={created_workspace.code} "
            f"user={user.code}"
        )

        return {
            "id": created_workspace.id,
            "code": created_workspace.code,
            "name": created_workspace.name,
            "description": created_workspace.description,
            "tenant_code": tenant.code,
            "status": created_workspace.status,
            "is_active": created_workspace.is_active,
            "created_at": created_workspace.created_at,
            "creator_user_code": user.code,
            "creator_role": WorkspaceRoleEnum.owner,
            "message": "Workspace created successfully and creator added as owner",
        }

    # ─── add users ─────────────────────────────────────────────────────────
    async def add_users(
        self,
        caller: UserMstModel,
        tenant: TenantsMstModel,
        workspace_code: str,
        data: AddUsersToWorkspaceRequest,
    ) -> Dict[str, Any]:
        """
        Best-effort bulk add: per-user failures don't abort the whole
        request. Returns added/skipped/failed buckets.
        """
        WorkspaceValidator.validate_add_users_request(
            workspace_code=workspace_code,
            users=data.users,
        )

        # Tenant-isolated lookup — 404 covers both "missing" and
        # "exists in another tenant" so we don't leak existence.
        workspace = await self.workspace_repo.get_by_code_and_tenant(
            code=workspace_code,
            tenant_code=tenant.code,
        )
        if workspace is None:
            raise WorkspaceServiceError(
                status_code=404,
                detail=f"Workspace '{workspace_code}' not found",
            )

        if workspace.status == WorkspaceStatusEnum.archived:
            raise WorkspaceServiceError(
                status_code=409,
                detail="Cannot add users to an archived workspace",
            )

        # Permission gate: org owner OR caller has owner/admin role here.
        if not caller.is_org_owner:
            caller_role = await self.mapping_repo.caller_role_in_workspace(
                workspace_code=workspace.code,
                user_mst_code=caller.code,
            )
            if caller_role not in (
                WorkspaceRoleEnum.owner,
                WorkspaceRoleEnum.admin,
            ):
                raise WorkspaceServiceError(
                    status_code=403,
                    detail=(
                        "Only org owners or workspace owners/admins can add "
                        "users to this workspace"
                    ),
                )

        added: list = []
        skipped: list = []
        failed: list = []
        to_create: list = []

        for entry in data.users:
            user_code = entry.user_code
            role = entry.role

            target_user = await self.user_repo.get_by_code(user_code)
            if target_user is None:
                failed.append({"user_code": user_code, "reason": "user not found or inactive"})
                continue

            if target_user.tenants_mst_code != tenant.code:
                failed.append({
                    "user_code": user_code,
                    "reason": "user does not belong to this organization",
                })
                continue

            existing_mapping = await self.mapping_repo.get_mapping(
                workspace_code=workspace.code,
                user_mst_code=target_user.code,
            )
            if existing_mapping is not None:
                skipped.append({
                    "user_code": user_code,
                    "role": existing_mapping.role,
                })
                continue

            mapping_data = make_workspace_user_mapping(
                workspace_code=workspace.code,
                workspace_name=workspace.name,
                user_mst_code=target_user.code,
                user_email=target_user.email_id,
                tenant_code=tenant.code,
                role=role,
            )
            to_create.append(mapping_data)
            added.append({"user_code": user_code, "role": role})

        if to_create:
            await self.mapping_repo.bulk_create(to_create)

        logger.info(
            f"add_users workspace={workspace.code} added={len(added)} "
            f"skipped={len(skipped)} failed={len(failed)}"
        )

        return {
            "workspace_code": workspace.code,
            "added": added,
            "skipped": skipped,
            "failed": failed,
            "total_requested": len(data.users),
            "total_added": len(added),
            "total_skipped": len(skipped),
            "total_failed": len(failed),
        }

    # ─── remove member ─────────────────────────────────────────────────────
    async def remove_member(
        self,
        caller: UserMstModel,
        tenant: TenantsMstModel,
        workspace_code: str,
        target_user_code: str,
    ) -> Dict[str, Any]:
        """
        Soft-remove a member from a workspace. Permission gate matches
        add_users (org owner OR caller has owner/admin role here). The
        workspace `owner` cannot be removed via this path.
        """
        workspace = await self.workspace_repo.get_by_code_and_tenant(
            code=workspace_code,
            tenant_code=tenant.code,
        )
        if workspace is None:
            raise WorkspaceServiceError(
                status_code=404,
                detail=f"Workspace '{workspace_code}' not found",
            )

        if workspace.status == WorkspaceStatusEnum.archived:
            raise WorkspaceServiceError(
                status_code=409,
                detail="Cannot remove members from an archived workspace",
            )

        if not caller.is_org_owner:
            caller_role = await self.mapping_repo.caller_role_in_workspace(
                workspace_code=workspace.code,
                user_mst_code=caller.code,
            )
            if caller_role not in (
                WorkspaceRoleEnum.owner,
                WorkspaceRoleEnum.admin,
            ):
                raise WorkspaceServiceError(
                    status_code=403,
                    detail=(
                        "Only org owners or workspace owners/admins can "
                        "remove members from this workspace"
                    ),
                )

        target_mapping = await self.mapping_repo.get_mapping(
            workspace_code=workspace.code,
            user_mst_code=target_user_code,
        )
        if target_mapping is None:
            raise WorkspaceServiceError(
                status_code=404,
                detail=(
                    f"User '{target_user_code}' is not a member of workspace "
                    f"'{workspace_code}'"
                ),
            )

        if target_mapping.role == WorkspaceRoleEnum.owner:
            raise WorkspaceServiceError(
                status_code=409,
                detail="The workspace owner cannot be removed",
            )

        await self.mapping_repo.soft_delete_mapping(
            workspace_code=workspace.code,
            user_mst_code=target_user_code,
        )

        logger.info(
            f"remove_member workspace={workspace.code} "
            f"target={target_user_code} caller={caller.code}"
        )

        return {
            "workspace_code": workspace.code,
            "user_code": target_user_code,
            "removed": True,
        }

    # ─── list members ──────────────────────────────────────────────────────
    async def list_members(
        self,
        tenant: TenantsMstModel,
        workspace_code: str,
    ) -> Dict[str, Any]:
        """
        Return active members of a workspace (joined with user_mst).
        Tenant-isolated lookup of the workspace first; returns 404 if the
        workspace does not exist in the caller's tenant.
        """
        workspace = await self.workspace_repo.get_by_code_and_tenant(
            code=workspace_code,
            tenant_code=tenant.code,
        )
        if workspace is None:
            raise WorkspaceServiceError(
                status_code=404,
                detail=f"Workspace '{workspace_code}' not found",
            )

        members = await self.mapping_repo.list_members(workspace_code=workspace.code)
        return {
            "workspace_code": workspace.code,
            "total": len(members),
            "members": members,
        }

    # ─── list eligible users ───────────────────────────────────────────────
    async def list_eligible_users(
        self,
        tenant: TenantsMstModel,
        workspace_code: str,
    ) -> Dict[str, Any]:
        """
        Return tenant users who are NOT yet mapped to this workspace.
        Used by the Add-Users picker.
        """
        workspace = await self.workspace_repo.get_by_code_and_tenant(
            code=workspace_code,
            tenant_code=tenant.code,
        )
        if workspace is None:
            raise WorkspaceServiceError(
                status_code=404,
                detail=f"Workspace '{workspace_code}' not found",
            )

        users = await self.user_repo.list_eligible_for_workspace(
            tenant_code=tenant.code,
            workspace_code=workspace.code,
        )
        return {
            "workspace_code": workspace.code,
            "total": len(users),
            "users": [
                {
                    "user_code": u.code,
                    "first_name": u.first_name,
                    "last_name": u.last_name,
                    "email_id": u.email_id,
                    "is_org_owner": u.is_org_owner,
                }
                for u in users
            ],
        }

    # ─── user-scoped list ──────────────────────────────────────────────────
    async def get_user_workspaces(
        self,
        user_code: str,
        tenant_code: str,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        logger.info(
            f"Fetching workspaces for user {user_code} in tenant {tenant_code}",
            extra={"user_code": user_code, "tenant_code": tenant_code},
        )

        result = await self.workspace_repo.get_user_workspaces(
            user_code=user_code,
            tenant_code=tenant_code,
            is_active=is_active,
            skip=skip,
            limit=limit,
        )

        logger.info(
            f"Found {result['total']} workspaces for user {user_code}",
            extra={"user_code": user_code, "count": result["total"]},
        )
        return result

    async def verify_app_workspace_access(
        self,
        user_code: str,
        tenant_code: str,
        application_code: str,
    ) -> bool:
        """
        Check whether the user has access to the workspace that owns the given application.

        Steps:
          1. Look up applications_mst.workspace_code for the application.
          2. If the application has no workspace assigned → deny (return False).
          3. Check workspace_user_mapping for an active row for (user, workspace, tenant).

        Returns True if access is granted, False otherwise.
        """
        stmt = select(ApplicationsMstModel.workspace_code).where(
            ApplicationsMstModel.code == application_code,
            ApplicationsMstModel.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        workspace_code = result.scalar_one_or_none()

        if not workspace_code:
            logger.warning(
                f"Application {application_code} has no workspace assigned — access denied",
                extra={"application_code": application_code},
            )
            return False

        mapping_stmt = select(WorkspaceUserMapModel).where(
            WorkspaceUserMapModel.workspace_code == workspace_code,
            WorkspaceUserMapModel.user_mst_code == user_code,
            WorkspaceUserMapModel.tenants_mst_code == tenant_code,
            WorkspaceUserMapModel.is_active == True,
            WorkspaceUserMapModel.is_deleted == False,
        )
        mapping_result = await self.session.execute(mapping_stmt)
        has_access = mapping_result.scalar_one_or_none() is not None

        if not has_access:
            logger.warning(
                f"User {user_code} has no access to workspace {workspace_code} "
                f"(application {application_code})",
                extra={
                    "user_code": user_code,
                    "workspace_code": workspace_code,
                    "application_code": application_code,
                },
            )
        return has_access

    async def can_write_for_workspace(
        self,
        user_code: str,
        tenant_code: str,
        workspace_code: str,
    ) -> bool:
        """Returns True if user has owner/admin/edit role (not read_only) in the workspace."""
        role = await self.mapping_repo.caller_role_in_workspace(
            workspace_code=workspace_code,
            user_mst_code=user_code,
        )
        return role is not None and role != WorkspaceRoleEnum.read_only

    async def get_user_workspace_codes(
        self,
        user_code: str,
        tenant_code: str,
    ) -> List[str]:
        """
        Returns all workspace codes the user has an active mapping row for
        within the given tenant.
        """
        stmt = select(WorkspaceUserMapModel.workspace_code).where(
            WorkspaceUserMapModel.user_mst_code == user_code,
            WorkspaceUserMapModel.tenants_mst_code == tenant_code,
            WorkspaceUserMapModel.is_active == True,
            WorkspaceUserMapModel.is_deleted == False,
        )
        result = await self.session.execute(stmt)
        return [row[0] for row in result.all()]

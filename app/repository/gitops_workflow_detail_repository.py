"""
GitOps Workflow Detail repository with specific workflow operations
"""

from typing import Optional, List, Tuple
from datetime import date
from sqlalchemy import select, func
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.gitops_workflow_detail_model import GitopsWorkflowDetailModel
from app.db.models.user_mst_model import UserMstModel
from app.db.models.service_config_model import ServiceConfigModel
from app.db.models.service_config_dockerfile_workflow_model import ServiceConfigDockerfileWorkflowModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.infrastructure_mst_model import InfrastructureMstModel
from app.db.models.alert_config_model import AlertConfigModel
from app.repository.base_repository import BaseRepository
from app.core.enum import PRStatusEnum, WorkflowSourceTableEnum


class GitopsWorkflowDetailRepository(BaseRepository[GitopsWorkflowDetailModel]):
    """
    Repository for GitOps Workflow Detail operations.

    Handles CRUD operations for tracking GitHub PRs and workflow executions.
    """

    def __init__(self, session: AsyncSession):
        super().__init__(GitopsWorkflowDetailModel, session)

    def _extract_case_type_from_infrastructure_code(self, infrastructure_code: str) -> Optional[str]:
        """
        Extract case type from infrastructure_mst.code.

        The code format is: {case_type}-{rest}
        Examples:
          - s3-bucket-logs-dev → s3-bucket
          - sqs-queue-orders-prod → sqs-queue
          - dynamodb-table-users → dynamodb-table

        Args:
            infrastructure_code: The infrastructure code to parse

        Returns:
            Case type (first two hyphen-separated parts) or None if invalid format
        """
        if not infrastructure_code:
            return None

        parts = infrastructure_code.split('-')
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}"

        return None

    def _extract_case_type_from_infrastructure_type_ref(self, infrastructure_type_ref_code: str) -> Optional[str]:
        """
        Extract case type from infrastructuretype_ref_code.

        The format is: {type}_infrastructuretype_ref
        Examples:
          - s3_infrastructuretype_ref → s3
          - sqs_infrastructuretype_ref → sqs
          - dynamodb_infrastructuretype_ref → dynamodb
          - lambda_infrastructuretype_ref → lambda

        Args:
            infrastructure_type_ref_code: The infrastructure type ref code to parse

        Returns:
            Case type or None if invalid format
        """
        if not infrastructure_type_ref_code:
            return None

        # Remove the "_infrastructuretype_ref" suffix
        if infrastructure_type_ref_code.endswith('_infrastructuretype_ref'):
            return infrastructure_type_ref_code.replace('_infrastructuretype_ref', '')

        return None

    async def get_by_codes(self, codes: List[str]) -> List[GitopsWorkflowDetailModel]:
        """
        Get workflow details by a list of workflow codes.

        Args:
            codes: List of gitops_workflow_detail codes

        Returns:
            List of matching GitopsWorkflowDetailModel records
        """
        if not codes:
            return []
        stmt = (
            select(self.model)
            .where(
                self.model.code.in_(codes),
                self.model.is_deleted == False
            )
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_pr_number(self, pr_number: int) -> Optional[GitopsWorkflowDetailModel]:
        """
        Get workflow detail by GitHub PR number.

        Args:
            pr_number: GitHub pull request number

        Returns:
            GitopsWorkflowDetailModel if found, None otherwise

        Example:
            workflow = await repo.get_by_pr_number(456)
            if workflow:
                print(f"PR #{workflow.pr_number} has {len(workflow.alert_configs)} alerts")
        """
        stmt = select(self.model).where(self.model.pr_number == pr_number)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_by_pr_and_repository(
        self,
        pr_number: int,
        git_repository: str
    ) -> Optional[GitopsWorkflowDetailModel]:
        """
        Get the latest workflow detail by PR number AND repository.

        Args:
            pr_number: GitHub pull request number
            git_repository: Repository in format "owner/repo"

        Returns:
            Latest GitopsWorkflowDetailModel (by created_at) if found, None otherwise
        """
        stmt = (
            select(self.model)
            .where(
                self.model.pr_number == pr_number,
                self.model.git_repository == git_repository
            )
            .order_by(self.model.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_by_pr_and_repository(
        self,
        pr_number: int,
        git_repository: str
    ) -> List[GitopsWorkflowDetailModel]:
        """
        Get all workflow details by PR number AND repository.

        Args:
            pr_number: GitHub pull request number
            git_repository: Repository in format "owner/repo"

        Returns:
            List of GitopsWorkflowDetailModel ordered by created_at DESC (newest first)

        Example:
            workflows = await repo.get_all_by_pr_and_repository(456, "org/repo")
            for workflow in workflows:
                print(f"Workflow: {workflow.code}")
        """
        stmt = (
            select(self.model)
            .where(
                self.model.pr_number == pr_number,
                self.model.git_repository == git_repository
            )
            .order_by(self.model.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_workflow_run_id(
        self,
        workflow_run_id: str
    ) -> Optional[GitopsWorkflowDetailModel]:
        """
        Get workflow detail by GitHub Actions workflow run ID.

        Args:
            workflow_run_id: GitHub Actions workflow run ID

        Returns:
            GitopsWorkflowDetailModel if found, None otherwise

        Example:
            workflow = await repo.get_by_workflow_run_id("98765432")
            if workflow:
                print(f"Workflow started at: {workflow.run_initiated_at}")
        """
        stmt = select(self.model).where(
            self.model.workflow_run_id == workflow_run_id
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_git_commit_sha(
        self,
        commit_sha: str
    ) -> Optional[GitopsWorkflowDetailModel]:
        """
        Get workflow detail by Git commit SHA.

        Args:
            commit_sha: Git commit SHA

        Returns:
            GitopsWorkflowDetailModel if found, None otherwise

        Example:
            workflow = await repo.get_by_git_commit_sha("a1b2c3d4...")
            if workflow:
                print(f"Commit deployed via PR #{workflow.pr_number}")
        """
        stmt = select(self.model).where(self.model.git_commit_sha == commit_sha)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, workflow_id: int) -> Optional[GitopsWorkflowDetailModel]:
        """
        Get GitOps workflow by ID.

        Args:
            workflow_id: Workflow ID

        Returns:
            GitopsWorkflowDetailModel if found, None otherwise

        Example:
            workflow = await repo.get_by_id(42)
            if workflow:
                print(f"PR #{workflow.pr_number}: {workflow.pr_url}")
        """
        stmt = select(self.model).where(self.model.id == workflow_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_workflows_with_filters(
        self,
        tenant_code: str,
        pr_status: Optional[PRStatusEnum] = None,
        user_code: Optional[str] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        service_code: Optional[str] = None,
        case_type: Optional[str] = None,
        page: int = 1,
        limit: int = 20
    ) -> Tuple[List[GitopsWorkflowDetailModel], int, dict, dict]:
        """
        List GitOps workflows with filters and pagination.

        Filters by tenant, optionally by PR status, user, date range, service code, and case type.
        Returns workflows with user relationship loaded for email/name access.
        Also returns service information and case type information for each workflow.

        Args:
            tenant_code: Tenant code (required - from JWT)
            pr_status: Optional PR status filter (PR_OPEN, PR_MERGED, PR_CLOSED)
            user_code: Optional user code filter
            date_from: Optional start date filter (inclusive)
            date_to: Optional end date filter (inclusive)
            service_code: Optional service code filter
            case_type: Optional case type filter (for infrastructure workflows)
            page: Page number (1-indexed)
            limit: Items per page

        Returns:
            Tuple of (list of workflows, total count, service_info dict, case_type_info dict)
            service_info maps workflow_id -> {"service_code": str, "service_name": str}
            case_type_info maps workflow_id -> {"case_type": str}

        Example:
            workflows, total, service_info, case_type_info = await repo.list_workflows_with_filters(
                tenant_code="aspora",
                pr_status=PRStatusEnum.PR_OPEN,
                service_code="SVC001",
                case_type="s3-bucket",
                date_from=date(2025, 1, 1),
                date_to=date(2025, 1, 31),
                page=1,
                limit=20
            )
        """
        # Build base query with user relationship and service joins
        # We'll select workflow IDs first, then get full objects with relationships
        base_query = (
            select(self.model.id)
            .where(
                self.model.tenant_mst_code == tenant_code,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
        )

        # Apply optional filters to base query
        if pr_status:
            base_query = base_query.where(self.model.pr_status == pr_status)

        if user_code:
            base_query = base_query.where(self.model.user_mst_code == user_code)

        if date_from:
            base_query = base_query.where(func.date(self.model.created_at) >= date_from)

        if date_to:
            base_query = base_query.where(func.date(self.model.created_at) <= date_to)

        # Service filter - need to join through multiple tables
        if service_code:
            # SERVICE_CONFIG
            sc1_join = (
                self.model.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG
            )
            sc1_exists = (
                select(ServiceConfigModel.id)
                .where(
                    ServiceConfigModel.code == self.model.transaction_code,
                    ServiceConfigModel.services_mst_code == service_code
                )
                .exists()
            )

            # SERVICE_CONFIG_DOCKERFILE
            sc2_join = (
                self.model.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE
            )
            sc2_exists = (
                select(ServiceConfigDockerfileWorkflowModel.id)
                .join(ServiceConfigModel, ServiceConfigDockerfileWorkflowModel.service_config_id == ServiceConfigModel.id)
                .where(
                    ServiceConfigDockerfileWorkflowModel.gitops_workflow_id == self.model.id,
                    ServiceConfigModel.services_mst_code == service_code
                )
                .exists()
            )

            base_query = base_query.where(
                (sc1_join & sc1_exists) | (sc2_join & sc2_exists)
            )

        # Case type filter - for INFRASTRUCTURE and KONG_ROUTE workflows
        if case_type:
            if case_type == "kong":
                # Filter by KONG_ROUTE table
                base_query = base_query.where(
                    self.model.table_name == WorkflowSourceTableEnum.KONG_ROUTE
                )
            else:
                # Filter by infrastructure type (s3, sqs, dynamodb, etc.)
                infra_join = (
                    self.model.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE
                )
                infra_exists = (
                    select(InfrastructureMstModel.id)
                    .where(
                        InfrastructureMstModel.code == self.model.transaction_code,
                        InfrastructureMstModel.infrastructuretype_ref_code == f'{case_type}_infrastructuretype_ref'
                    )
                    .exists()
                )
                base_query = base_query.where(infra_join & infra_exists)

        # Count query - same filters as base_query
        count_query = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.tenant_mst_code == tenant_code,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
        )

        if pr_status:
            count_query = count_query.where(self.model.pr_status == pr_status)

        if user_code:
            count_query = count_query.where(self.model.user_mst_code == user_code)

        if date_from:
            count_query = count_query.where(func.date(self.model.created_at) >= date_from)

        if date_to:
            count_query = count_query.where(func.date(self.model.created_at) <= date_to)

        if service_code:
            # Same service filter logic
            sc1_join = self.model.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG
            sc1_exists = (
                select(ServiceConfigModel.id)
                .where(
                    ServiceConfigModel.code == self.model.transaction_code,
                    ServiceConfigModel.services_mst_code == service_code
                )
                .exists()
            )

            sc2_join = self.model.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE
            sc2_exists = (
                select(ServiceConfigDockerfileWorkflowModel.id)
                .join(ServiceConfigModel, ServiceConfigDockerfileWorkflowModel.service_config_id == ServiceConfigModel.id)
                .where(
                    ServiceConfigDockerfileWorkflowModel.gitops_workflow_id == self.model.id,
                    ServiceConfigModel.services_mst_code == service_code
                )
                .exists()
            )

            count_query = count_query.where(
                (sc1_join & sc1_exists) | (sc2_join & sc2_exists)
            )

        if case_type:
            if case_type == "kong":
                # Filter by KONG_ROUTE table
                count_query = count_query.where(
                    self.model.table_name == WorkflowSourceTableEnum.KONG_ROUTE
                )
            else:
                # Filter by infrastructure type (s3, sqs, dynamodb, etc.)
                infra_join = self.model.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE
                infra_exists = (
                    select(InfrastructureMstModel.id)
                    .where(
                        InfrastructureMstModel.code == self.model.transaction_code,
                        InfrastructureMstModel.infrastructuretype_ref_code == f'{case_type}_infrastructuretype_ref'
                    )
                    .exists()
                )
                count_query = count_query.where(infra_join & infra_exists)

        count_result = await self.session.execute(count_query)
        total = count_result.scalar() or 0

        # Apply pagination and ordering to get workflow IDs
        offset = (page - 1) * limit
        paginated_ids_query = (
            base_query
            .order_by(self.model.created_at.desc())
            .offset(offset)
            .limit(limit)
        )

        ids_result = await self.session.execute(paginated_ids_query)
        workflow_ids = [row[0] for row in ids_result.all()]

        # Fetch full workflow objects with user relationship
        if not workflow_ids:
            return [], 0, {}, {}

        workflows_query = (
            select(self.model)
            .options(joinedload(self.model.user))
            .where(self.model.id.in_(workflow_ids))
            .order_by(self.model.created_at.desc())
        )

        workflows_result = await self.session.execute(workflows_query)
        workflows = list(workflows_result.scalars().unique().all())

        # Fetch service information for all workflows
        service_info = {}
        for workflow in workflows:
            service_code = None
            service_name = None

            if workflow.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG:
                # Join through service_configs
                sc_stmt = (
                    select(ServicesMstModel.code, ServicesMstModel.name)
                    .join(ServiceConfigModel, ServicesMstModel.code == ServiceConfigModel.services_mst_code)
                    .where(ServiceConfigModel.code == workflow.transaction_code)
                )
                sc_result = await self.session.execute(sc_stmt)
                sc_row = sc_result.first()
                if sc_row:
                    service_code, service_name = sc_row

            elif workflow.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE:
                # Join through service_config_dockerfile_workflows
                scdw_stmt = (
                    select(ServicesMstModel.code, ServicesMstModel.name)
                    .join(ServiceConfigModel, ServicesMstModel.code == ServiceConfigModel.services_mst_code)
                    .join(ServiceConfigDockerfileWorkflowModel, ServiceConfigModel.id == ServiceConfigDockerfileWorkflowModel.service_config_id)
                    .where(ServiceConfigDockerfileWorkflowModel.gitops_workflow_id == workflow.id)
                )
                scdw_result = await self.session.execute(scdw_stmt)
                scdw_row = scdw_result.first()
                if scdw_row:
                    service_code, service_name = scdw_row

            service_info[workflow.id] = {
                "service_code": service_code,
                "service_name": service_name
            }

        # Build case_type_info for infrastructure and Kong workflows
        case_type_info = {}
        for workflow in workflows:
            if workflow.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE:
                # Join with infrastructure_mst to get infrastructuretype_ref_code
                infra_stmt = (
                    select(InfrastructureMstModel.infrastructuretype_ref_code)
                    .where(InfrastructureMstModel.code == workflow.transaction_code)
                )
                infra_result = await self.session.execute(infra_stmt)
                infra_type_ref_row = infra_result.first()

                if infra_type_ref_row and infra_type_ref_row[0]:
                    case_type = self._extract_case_type_from_infrastructure_type_ref(infra_type_ref_row[0])
                    case_type_info[workflow.id] = {
                        "case_type": case_type
                    }
            elif workflow.table_name == WorkflowSourceTableEnum.KONG_ROUTE:
                # Kong routes have case_type = "kong"
                case_type_info[workflow.id] = {
                    "case_type": "kong"
                }

        return workflows, total, service_info, case_type_info

    async def get_users_with_workflows(
        self,
        tenant_code: str
    ) -> List[dict]:
        """
        Get list of users who have created workflows for a tenant.

        Used for user filter dropdown in frontend.

        Args:
            tenant_code: Tenant code

        Returns:
            List of dicts with user_code, user_email, user_name

        Example:
            users = await repo.get_users_with_workflows("aspora")
            # [{"user_code": "USR001", "user_email": "john@example.com", "user_name": "John"}]
        """
        stmt = (
            select(
                self.model.user_mst_code,
                UserMstModel.email_id,
                UserMstModel.name
            )
            .join(UserMstModel, self.model.user_mst_code == UserMstModel.code, isouter=True)
            .where(
                self.model.tenant_mst_code == tenant_code,
                self.model.is_deleted == False,
                self.model.user_mst_code.isnot(None)
            )
            .distinct()
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        return [
            {
                "user_code": row[0],
                "user_email": row[1],
                "user_name": row[2]
            }
            for row in rows
        ]

    async def get_services_with_workflows(
        self,
        tenant_code: str
    ) -> List[dict]:
        """
        Get distinct services that have workflows for the service filter dropdown.

        Args:
            tenant_code: Tenant code

        Returns:
            List of dicts with service_code and service_name

        Example:
            services = await repo.get_services_with_workflows("aspora")
            # [{"service_code": "SVC001", "service_name": "API Gateway"}, ...]
        """
        # Single optimized query with LEFT OUTER JOINs to get all distinct services
        # for SERVICE_CONFIG workflows
        sc_services = (
            select(ServicesMstModel.code, ServicesMstModel.name)
            .join(ServiceConfigModel, ServicesMstModel.code == ServiceConfigModel.services_mst_code)
            .join(self.model, ServiceConfigModel.code == self.model.transaction_code)
            .where(
                self.model.tenant_mst_code == tenant_code,
                self.model.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
            .distinct()
        )

        # For SERVICE_CONFIG_DOCKERFILE workflows
        dockerfile_services = (
            select(ServicesMstModel.code, ServicesMstModel.name)
            .join(ServiceConfigModel, ServicesMstModel.code == ServiceConfigModel.services_mst_code)
            .join(ServiceConfigDockerfileWorkflowModel, ServiceConfigModel.id == ServiceConfigDockerfileWorkflowModel.service_config_id)
            .join(self.model, ServiceConfigDockerfileWorkflowModel.gitops_workflow_id == self.model.id)
            .where(
                self.model.tenant_mst_code == tenant_code,
                self.model.table_name == WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
            .distinct()
        )

        # Use UNION to combine both queries and get distinct results
        combined_query = sc_services.union(dockerfile_services).order_by(ServicesMstModel.name)

        result = await self.session.execute(combined_query)
        rows = result.all()

        return [
            {
                "service_code": row[0],
                "service_name": row[1]
            }
            for row in rows
        ]

    async def get_case_types_with_workflows(
        self,
        tenant_code: str
    ) -> List[dict]:
        """
        Get distinct case types for infrastructure and Kong workflows.

        Args:
            tenant_code: Tenant code

        Returns:
            List of dicts with case_type

        Example:
            case_types = await repo.get_case_types_with_workflows("aspora")
            # [{"case_type": "s3"}, {"case_type": "sqs"}, {"case_type": "kong"}, ...]
        """
        case_types = set()

        # Get infrastructure case types
        stmt = (
            select(InfrastructureMstModel.infrastructuretype_ref_code)
            .select_from(self.model)
            .join(
                InfrastructureMstModel,
                InfrastructureMstModel.code == self.model.transaction_code
            )
            .where(
                self.model.tenant_mst_code == tenant_code,
                self.model.table_name == WorkflowSourceTableEnum.INFRASTRUCTURE,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
            .distinct()
        )

        result = await self.session.execute(stmt)
        infrastructure_type_refs = [row[0] for row in result.all()]

        # Extract unique case types from infrastructuretype_ref_code
        for type_ref in infrastructure_type_refs:
            case_type = self._extract_case_type_from_infrastructure_type_ref(type_ref)
            if case_type:
                case_types.add(case_type)

        # Check if there are any KONG_ROUTE workflows
        kong_stmt = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.tenant_mst_code == tenant_code,
                self.model.table_name == WorkflowSourceTableEnum.KONG_ROUTE,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
        )

        kong_result = await self.session.execute(kong_stmt)
        kong_count = kong_result.scalar() or 0

        if kong_count > 0:
            case_types.add("kong")

        return [{"case_type": ct} for ct in sorted(case_types)]

    async def get_open_pr_by_transaction(
        self,
        transaction_code: str,
        table_name: WorkflowSourceTableEnum,
        tenant_mst_code: str
    ) -> Optional[GitopsWorkflowDetailModel]:
        """
        Check if there's an open PR for a given transaction (service config or dockerfile).

        Used to prevent creating duplicate PRs when user already has an open PR
        for the same service config.

        Args:
            transaction_code: The transaction code (service_config.code or dockerfile workflow code)
            table_name: The table name enum (SERVICE_CONFIG or SERVICE_CONFIG_DOCKERFILE)
            tenant_mst_code: Tenant code for scoping

        Returns:
            GitopsWorkflowDetailModel if open PR exists, None otherwise

        Example:
            existing_pr = await repo.get_open_pr_by_transaction(
                transaction_code="service-config-casa-stage-london-no-alb",
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                tenant_mst_code="aspora"
            )
            if existing_pr:
                raise HTTPException(409, f"Open PR #{existing_pr.pr_number} exists")
        """
        stmt = (
            select(self.model)
            .where(
                self.model.transaction_code == transaction_code,
                self.model.table_name == table_name,
                self.model.tenant_mst_code == tenant_mst_code,
                self.model.pr_status == PRStatusEnum.PR_OPEN,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
            .order_by(self.model.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_open_pr_by_service_alerts(
        self,
        service_code: str,
        tenant_mst_code: str
    ) -> Optional[GitopsWorkflowDetailModel]:
        """
        Find any open PR for any alert belonging to a service.

        Used to unify PRs across single and bulk alert endpoints - both endpoints
        will reuse the same PR for a given service instead of creating separate PRs.

        Query joins gitops_workflow_detail with alert_configs to find PRs
        where the alert belongs to the specified service.

        Args:
            service_code: The service code (services_mst.code)
            tenant_mst_code: Tenant code for scoping

        Returns:
            GitopsWorkflowDetailModel if open PR exists for any alert of this service,
            None otherwise

        Example:
            # Find any open PR for alerts belonging to service "qurser"
            existing_pr = await repo.get_open_pr_by_service_alerts(
                service_code="cb5633b2-0ed0-4975-a3ee-731a8da1d6c3",
                tenant_mst_code="vance"
            )
            if existing_pr:
                # Reuse this PR for new alert changes
                print(f"Found existing PR #{existing_pr.pr_number}")
        """
        stmt = (
            select(self.model)
            .join(
                AlertConfigModel,
                self.model.transaction_code == AlertConfigModel.code
            )
            .where(
                AlertConfigModel.services_mst_code == service_code,
                self.model.table_name == WorkflowSourceTableEnum.ALERT_CONFIG,
                self.model.tenant_mst_code == tenant_mst_code,
                self.model.pr_status == PRStatusEnum.PR_OPEN,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
            .order_by(self.model.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_transaction_code_and_table(
        self,
        transaction_code: str,
        table_name: WorkflowSourceTableEnum,
        tenant_code: str,
        page: int = 1,
        limit: int = 20
    ) -> Tuple[List[GitopsWorkflowDetailModel], int]:
        """
        Get workflow details by polymorphic reference (transaction_code + table_name).

        Used for PR history modal - retrieves all PRs for a specific entity.

        Args:
            transaction_code: Code of the source entity (e.g., service_config.code for Terragrunt,
                             dockerfile_workflow_code for Dockerfile)
            table_name: Source table enum (SERVICE_CONFIG, SERVICE_CONFIG_DOCKERFILE, etc.)
            tenant_code: Tenant code for isolation
            page: Page number (1-indexed)
            limit: Items per page

        Returns:
            Tuple of (list of workflows ordered by created_at DESC, total count)

        Example:
            # Get Terragrunt PR history for a service config
            workflows, total = await repo.get_by_transaction_code_and_table(
                transaction_code="service-config-abc-dev-london-existing_alb",
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                tenant_code="vance",
                page=1,
                limit=20
            )

            # Get Dockerfile PR history for a specific branch
            workflows, total = await repo.get_by_transaction_code_and_table(
                transaction_code="SCDF_f1672d17",
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
                tenant_code="vance",
                page=1,
                limit=20
            )
        """
        # Base query with user relationship for email/name
        base_query = (
            select(self.model)
            .options(joinedload(self.model.user))
            .where(
                self.model.transaction_code == transaction_code,
                self.model.table_name == table_name,
                self.model.tenant_mst_code == tenant_code,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
        )

        # Count query
        count_query = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.transaction_code == transaction_code,
                self.model.table_name == table_name,
                self.model.tenant_mst_code == tenant_code,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
        )

        count_result = await self.session.execute(count_query)
        total = count_result.scalar() or 0

        # Apply pagination and ordering (newest first)
        offset = (page - 1) * limit
        paginated_query = (
            base_query
            .order_by(self.model.created_at.desc())
            .offset(offset)
            .limit(limit)
        )

        result = await self.session.execute(paginated_query)
        workflows = list(result.scalars().unique().all())

        return workflows, total

    async def get_by_transaction_codes_and_table(
        self,
        transaction_codes: List[str],
        table_name: WorkflowSourceTableEnum,
        tenant_code: str,
        page: int = 1,
        limit: int = 20
    ) -> Tuple[List[GitopsWorkflowDetailModel], int]:
        """
        Get workflow details by multiple transaction codes (polymorphic reference).

        Used for fetching workflows for multiple entities at once (e.g., all Dockerfile PRs
        for a service config, or all Pipeline PRs for a service+environment combination).

        Args:
            transaction_codes: List of transaction codes (e.g., dockerfile workflow codes, pipeline codes)
            table_name: Source table enum (SERVICE_CONFIG_DOCKERFILE, PIPELINE, etc.)
            tenant_code: Tenant code for isolation
            page: Page number (1-indexed)
            limit: Items per page

        Returns:
            Tuple of (list of workflows ordered by created_at DESC, total count)

        Example:
            # Get all Dockerfile PRs for a service config
            workflows, total = await repo.get_by_transaction_codes_and_table(
                transaction_codes=["SCDF_abc123", "SCDF_def456"],
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG_DOCKERFILE,
                tenant_code="vance",
                page=1,
                limit=20
            )

            # Get all Pipeline PRs for a service
            workflows, total = await repo.get_by_transaction_codes_and_table(
                transaction_codes=["pipeline_auth_dev_123", "pipeline_auth_dev_456"],
                table_name=WorkflowSourceTableEnum.PIPELINE,
                tenant_code="vance",
                page=1,
                limit=20
            )
        """
        if not transaction_codes:
            return [], 0

        # Base query with user relationship for email/name
        base_query = (
            select(self.model)
            .options(joinedload(self.model.user))
            .where(
                self.model.transaction_code.in_(transaction_codes),
                self.model.table_name == table_name,
                self.model.tenant_mst_code == tenant_code,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
        )

        # Count query
        count_query = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.transaction_code.in_(transaction_codes),
                self.model.table_name == table_name,
                self.model.tenant_mst_code == tenant_code,
                self.model.is_deleted == False,
                self.model.is_active == True
            )
        )

        count_result = await self.session.execute(count_query)
        total = count_result.scalar() or 0

        # Apply pagination and ordering (newest first)
        offset = (page - 1) * limit
        paginated_query = (
            base_query
            .order_by(self.model.created_at.desc())
            .offset(offset)
            .limit(limit)
        )

        result = await self.session.execute(paginated_query)
        workflows = list(result.scalars().unique().all())

        return workflows, total

    async def get_by_transaction_and_pr(
        self,
        transaction_code: str,
        table_name: WorkflowSourceTableEnum,
        pr_number: int,
        tenant_code: str
    ) -> Optional[GitopsWorkflowDetailModel]:
        """
        Get existing workflow by transaction_code, table_name, and pr_number.

        Used to check if a workflow record already exists for a specific PR
        before creating a new one (to avoid duplicates).

        Args:
            transaction_code: Code of the source entity
            table_name: Source table enum
            pr_number: GitHub PR number
            tenant_code: Tenant code for isolation

        Returns:
            GitopsWorkflowDetailModel if found, None otherwise

        Example:
            existing = await repo.get_by_transaction_and_pr(
                transaction_code="service-config-abc-dev",
                table_name=WorkflowSourceTableEnum.SERVICE_CONFIG,
                pr_number=863,
                tenant_code="vance"
            )
            if existing:
                # Update existing record instead of creating new one
                ...
        """
        stmt = (
            select(self.model)
            .where(
                self.model.transaction_code == transaction_code,
                self.model.table_name == table_name,
                self.model.pr_number == pr_number,
                self.model.tenant_mst_code == tenant_code,
                self.model.is_deleted == False
            )
            .order_by(self.model.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_pr_status(
        self,
        workflow_id: int,
        new_status: PRStatusEnum
    ) -> Optional[GitopsWorkflowDetailModel]:
        """
        Update the PR status of a workflow record.

        Used when replacing an old PR with a new one - marks the old PR as closed.

        Args:
            workflow_id: The workflow record ID to update
            new_status: The new PR status (e.g., PR_CLOSED)

        Returns:
            Updated GitopsWorkflowDetailModel if found, None otherwise

        Example:
            # Mark old PR as closed when superseded by new PR
            await repo.update_pr_status(
                workflow_id=old_workflow.id,
                new_status=PRStatusEnum.PR_CLOSED
            )
        """
        workflow = await self.get_by_id(workflow_id)
        if workflow:
            workflow.pr_status = new_status
            await self.session.flush()
            return workflow
        return None

    async def update_pr_status_by_pr_and_repository(
        self,
        pr_number: int,
        git_repository: str,
        new_status: PRStatusEnum
    ) -> int:
        """
        Update the PR status of all workflow records for a given PR number and repository.

        Used to sync DB status when GitHub reports the PR as merged/closed.

        Args:
            pr_number: GitHub pull request number
            git_repository: Repository in format "owner/repo"
            new_status: The new PR status (e.g., PR_MERGED, PR_CLOSED)

        Returns:
            Number of records updated

        Example:
            # Update all workflows when PR is merged on GitHub
            count = await repo.update_pr_status_by_pr_and_repository(
                pr_number=1280,
                git_repository="Regobs/terraform-test",
                new_status=PRStatusEnum.PR_MERGED
            )
        """
        workflows = await self.get_all_by_pr_and_repository(pr_number, git_repository)
        updated_count = 0

        for workflow in workflows:
            if workflow.pr_status != new_status:
                workflow.pr_status = new_status
                updated_count += 1

        if updated_count > 0:
            await self.session.flush()

        return updated_count

    async def get_distinct_contributors_by_transaction(
        self,
        transaction_code: str,
        tenant_code: str
    ) -> List[dict]:
        """
        Get distinct users who contributed to a specific transaction (service config).

        Used for collaborative PR feature - fetches all previous contributors
        to credit them as co-authors in new PRs.

        Args:
            transaction_code: Service config code
            tenant_code: Tenant code for isolation

        Returns:
            List of dicts with 'email', 'first_name', 'last_name'
        """
        stmt = (
            select(
                UserMstModel.email_id,
                UserMstModel.first_name,
                UserMstModel.last_name
            )
            .select_from(self.model)
            .join(UserMstModel, self.model.user_mst_code == UserMstModel.code)
            .where(
                self.model.transaction_code == transaction_code,
                self.model.tenant_mst_code == tenant_code,
                self.model.user_mst_code.isnot(None),
                UserMstModel.email_id.isnot(None),
                self.model.is_deleted == False,
                self.model.is_active == True
            )
            .distinct()
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        return [
            {
                "email": row[0],
                "first_name": row[1] or "",
                "last_name": row[2] or ""
            }
            for row in rows
        ]

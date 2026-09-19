"""
Aspora File Locator

Tenant-specific implementation for determining file locations.
Handles both service configs and standalone infrastructure for Aspora tenant.

This class contains the business logic specific to Aspora for:
- Determining which repositories files should go to
- Calculating file paths based on Aspora's conventions
- Handling multi-branch service configurations
"""

import logging
import re
import uuid
from typing import Dict, List, Optional
from app.schemas.file_location_response_schema import FileLocationResponse, FileLocationItem
from app.schemas.pr_workflow_context import PRWorkflowContext
from app.core.config import settings
from app.utils.timing import log_timing
from app.utils.workflow_file_helpers import resolve_eks_workflow

logger = logging.getLogger(__name__)

VANCE_ASPORA_TENANTS = {"vance", "aspora"}


class AsporaFileLocator:
    @staticmethod
    def _sanitize_identifier(identifier: str) -> str:
        """Sanitize identifier for file paths (replace consecutive spaces/underscores/hyphens with single hyphen)"""
        return re.sub(r'[\s_-]+', '-', identifier).strip('-').lower()

    @staticmethod
    def _denest_base_slug(base_branch_slug: str, repo_slug: str) -> str:
        """A generated branch slugifies to 'feature-infra-{repo}-{core}-{token}'.
        When one is reused as a base branch (e.g. selected as the service's
        branch), embedding it whole would grow every rerun's name by a full
        copy of the previous one, until git's 255-byte cap and the DB columns
        overflow. Peel the generated layers so only the original core is ever
        embedded. Name-only: the real base branch used for git is untouched."""
        token_re = re.compile(r"-[0-9a-f]{8}$")
        while base_branch_slug.startswith("feature-infra-"):
            base_branch_slug = base_branch_slug[len("feature-infra-"):]
            if repo_slug and base_branch_slug.startswith(f"{repo_slug}-"):
                base_branch_slug = base_branch_slug[len(repo_slug) + 1:]
            base_branch_slug = token_re.sub("", base_branch_slug)
        return base_branch_slug

    @staticmethod
    def _get_folder_env(environment: str) -> str:
        """Get folder environment name (e.g., dev, stage, qa, prod)"""
        env_map = {
            'dev': 'dev',
            'development': 'dev',
            'staging': 'stage',
            'stage': 'stage',
            'stg': 'stage',
            'qa': 'qa',
            'prod': 'prod',
            'production': 'prod'
        }
        return env_map.get(environment.lower(), environment.lower())

    @staticmethod
    def _normalize_region_for_path(region: str) -> str:
        """Normalize region for file path (remove 'aws-' prefix if present)"""
        if region.startswith('aws-'):
            return region[4:]
        return region

    @staticmethod
    def _get_aws_region_from_geo_loc(geo_loc: str) -> str:
        """
        Map business/deployment region (geo_loc) to AWS region.

        Args:
            geo_loc: Geographic location code (e.g., 'mumbai', 'london')

        Returns:
            AWS region code (e.g., 'ap-south-1', 'eu-west-2')
        """
        mapping = {
            "mumbai": "ap-south-1",
            "london": "eu-west-2",
            "uk": "eu-west-2",
            "us": "us-east-1",
            "aspora-mumbai": "ap-south-1",
            "aspora-london": "eu-west-2",
            "aspora-uk": "eu-west-2",
            "aspora-us": "us-east-1",
            "region-aspora-mumbai": "ap-south-1",
            "region-aspora-london": "eu-west-2",
            "region-aspora-us": "us-east-1",
        }
        return mapping.get(geo_loc.lower(), "ap-south-1")

    @staticmethod
    def _sanitize_dockerfile_path(path: str) -> str:
        """Sanitize dockerfile path (no leading /, no trailing /)"""
        if not path:
            return path
        return path.strip('/')

    @staticmethod
    def _get_branch_name() -> str:
        """
        Get the git branch name for Aspora tenant.

        For Aspora tenant, all environments go to 'stage' branch for PR-based workflows.

        Returns:
            Git branch name (always 'stage' for Aspora)
        """
        return "stage"

    @staticmethod
    def _normalize_environment_for_env_files(environment: str, tenant: str = "") -> str:
        """
        Normalize environment name for env file paths.

        Args:
            environment: Environment name
            tenant: Tenant code

        Returns:
            Normalized environment name (dev, stage, qa, prod)
        """
        env_lower = environment.lower()
        tenant_lower = tenant.lower() if tenant else ""

        if env_lower == "prod":
            return "prod"
        if env_lower == "stage":
            return "stage"
        if env_lower == "qa":
            return "qa"
        if tenant_lower in VANCE_ASPORA_TENANTS and env_lower == "staging":
            return "stage"
        return env_lower

    @staticmethod
    def _get_envs_folder_name(environment: str, tenant: str = "") -> str:
        """
        Get the envs folder name based on environment and tenant.

        For vance/aspora tenants in dev environment, uses 'envs-dev'.
        Otherwise uses 'envs'.

        Args:
            environment: Environment name
            tenant: Tenant code

        Returns:
            Folder name ('envs' or 'envs-dev')
        """
        tenant_lower = tenant.lower() if tenant else ""
        env_lower = environment.lower()

        if tenant_lower in VANCE_ASPORA_TENANTS and env_lower == "dev":
            return "envs-dev"
        return "envs"

    @staticmethod
    def _get_service_name_for_env_files(service_name: str) -> str:
        """
        Get service name with -service suffix for env file paths.

        Args:
            service_name: Service name (sanitized)

        Returns:
            Service name with -service suffix
        """
        if service_name.endswith("-service"):
            return service_name
        return f"{service_name}-service"

    @staticmethod
    def _resolve_k8s_cluster_folder(config_snapshot: dict) -> str:
        """
        Resolve the k8s-manifests cluster-scoped folder segment ("app"/"be")
        for a service's EKS cluster.

        Same CLUSTER_TYPE_MAP + DEFAULT_CLUSTER_TYPE fallback ("backend") the
        aspora workflow component already uses to pick a cluster type; the
        k8s-manifests folder names just differ from the "application"/
        "backend" labels used there.
        """
        from app.plugin.aspora.script_gen_components.aspora_workflow_script_gen_component import (
            CLUSTER_TYPE_MAP, DEFAULT_CLUSTER_TYPE,
        )
        resolved_type = (
            CLUSTER_TYPE_MAP.get(config_snapshot.get("cluster_arn", ""))
            or CLUSTER_TYPE_MAP.get(config_snapshot.get("cluster_name") or config_snapshot.get("ecs_cluster", ""))
            or CLUSTER_TYPE_MAP.get(config_snapshot.get("infrastructure_mst_code", ""))
            or DEFAULT_CLUSTER_TYPE
        )
        return {"application": "app", "backend": "be"}[resolved_type]

    @staticmethod
    async def _get_or_create_feature_branch(
        workflow_context: PRWorkflowContext,
        repo: str,
        base_branch: str,
        tenant: str,
        pr_type: str = "infrastructure",
    ) -> Optional[str]:
        """
        Get or create feature branch for a repo+base_branch combination.

        This method:
        1. Checks if feature branch exists in workflow_context
        2. If not, generates new feature branch name, stores it, and creates it in GitHub
        3. In conflict_resolve mode: returns None for non-target repos (skip silently)

        Args:
            workflow_context: PR workflow context
            repo: Repository URL or name (e.g., "regobs/terraform-infrastructure")
            base_branch: Base branch name (e.g., "stage", "main")
            tenant: Tenant code for GitOpsHandler
            pr_type: PR type — "infrastructure" | "workflow" | "k8s_manifest"

        Returns:
            str: Feature branch name (existing or newly generated), or None if skipped
        """
        from app.handlers.gitops_handler import GitOpsHandler

        # Create key: "{repo}|||{base_branch}" using triple-pipe as delimiter
        # This avoids conflicts with hyphens in repo names or branch names
        key = f"{repo}|||{base_branch}"

        _TYPE_PRIORITY = {"infrastructure": 3, "k8s_manifest": 2, "workflow": 1}

        if key not in workflow_context.feature_branches:
            # In conflict_resolve mode, skip repos that are not pre-populated
            if workflow_context.is_conflict_resolve:
                logger.info(f"Conflict resolve: skipping non-target repo/branch {key}")
                return None
            # Generate new feature branch name
            repo_slug = AsporaFileLocator._sanitize_identifier(repo.replace("/", "-"))
            base_branch_slug = AsporaFileLocator._sanitize_identifier(base_branch.replace("/", "-"))
            base_branch_slug = AsporaFileLocator._denest_base_slug(base_branch_slug, repo_slug)
            token = uuid.uuid4().hex[:8]
            prefix = "feature/infra/"
            body = f"{repo_slug}-{base_branch_slug}"
            max_body_len = 255 - len(prefix) - len(token) - 1
            if max_body_len < 1:
                max_body_len = 1
            if len(body) > max_body_len:
                body = body[:max_body_len].rstrip("-")
            if not body:
                body = "branch"
                if len(body) > max_body_len:
                    body = body[:max_body_len]
            feature_branch = f"{prefix}{body}-{token}"
            workflow_context.feature_branches[key] = {"branch": feature_branch, "type": pr_type}
            logger.info(f"Created new feature branch name for {key}: {feature_branch} (type={pr_type})")

            # Create feature branch in GitHub
            logger.info(f"Creating feature branch {feature_branch} in GitHub from {base_branch}")

            # Extract owner and repo from full repo path (e.g., "owner/repo")
            repo_parts = repo.split('/')
            owner = repo_parts[0] if len(repo_parts) > 1 else None
            repo_name = repo_parts[1] if len(repo_parts) > 1 else repo

            # Get GitOps component and create branch (needs DB session for token lookup)
            from app.db.session import AsyncSessionLocal as _ASL
            async with _ASL() as _db:
                component = GitOpsHandler.get_component(tenant, _db)
                timing_context = f"repo={repo_name} base={base_branch} feature={feature_branch}"
                with log_timing(logger, "feature_branch_create", context=timing_context):
                    branch_result = await component.create_branch(
                        owner=owner,
                        repo=repo_name,
                        base_branch=base_branch,
                        feature_branch=feature_branch
                    )
            logger.info(f"Feature branch creation result: {branch_result}")
        else:
            fb_data = workflow_context.feature_branches[key]
            feature_branch = fb_data["branch"] if isinstance(fb_data, dict) else fb_data
            # Upgrade type if this call carries higher priority
            if isinstance(fb_data, dict):
                if _TYPE_PRIORITY.get(pr_type, 0) > _TYPE_PRIORITY.get(fb_data.get("type", "infrastructure"), 0):
                    fb_data["type"] = pr_type
            logger.debug(f"Reusing existing feature branch for {key}: {feature_branch}")

        return feature_branch

    @classmethod
    async def _list_repo_files(
        cls,
        tenant: str,
        repo: str,
        branch: str,
        path: str,
    ) -> List[str]:
        """File names directly under `path` on `branch`.

        Best-effort: a missing directory, a revoked token or a GitHub outage
        returns an empty list. Callers use this to DISCOVER an optional file,
        so failing here would turn a degraded lookup into a failed deploy.
        """
        names: List[str] = []
        try:
            from app.handlers.gitops_handler import GitOpsHandler

            repo_parts = repo.split("/")
            owner = repo_parts[0] if len(repo_parts) > 1 else None
            repo_name = repo_parts[1] if len(repo_parts) > 1 else repo
            result = await GitOpsHandler.list_directory(
                tenant=tenant,
                owner=owner,
                repo=repo_name,
                path=path,
                branch=branch,
            )
            names = [
                entry.get("name")
                for entry in (result.get("entries") or [])
                if entry.get("type") == "file" and entry.get("name")
            ]
        except Exception:
            logger.warning(
                f"Could not list {path} in {repo}@{branch} — "
                "falling back to the generated workflow name",
                exc_info=True,
            )

        return names

    @classmethod
    async def _resolve_eks_workflow_path(
        cls,
        tenant: str,
        repo: str,
        feature_branch: str,
        base_branch: str,
        env: str,
        service_label: str,
        canonical_name: str,
        organization: str = "",
    ) -> str:
        """Path of the EKS workflow to write: the one this service already
        deploys through when there is one, otherwise the generated name.

        Services onboarded before DevLift named their workflow themselves —
        `deploy-{svc}-eks-prod.yml`, `eks-stage-deploy.yaml`. Committing the
        generated name beside such a file left the service with TWO workflows
        running on every push. Adopting the existing path instead routes the
        change into the file that is already live, and the workflow component
        then patches only its managed lines (it treats an existing file as an
        update), so the hand-written steps survive.

        A matching NAME is never enough to adopt a file — `deploy-eks-prod.yml`
        could belong to any service in the repo. Each candidate is opened and
        adopted only if its own `service_name` / `organization` /
        `environment` inputs say it is this service's pipeline.

        The feature branch is read first — it is cut from the base branch, so
        it carries everything the base has plus anything this run already
        staged — with the base branch as the fallback when that listing is
        empty (a branch that could not be read at all).
        """
        canonical_path = f".github/workflows/{canonical_name}"

        listing_branch = feature_branch
        names = await cls._list_repo_files(
            tenant, repo, feature_branch, ".github/workflows"
        )
        if not names and base_branch and base_branch != feature_branch:
            listing_branch = base_branch
            names = await cls._list_repo_files(
                tenant, repo, base_branch, ".github/workflows"
            )
        if not names:
            return canonical_path

        async def _fetch(name: str):
            from app.handlers.gitops_handler import GitOpsHandler

            repo_parts = repo.split("/")
            owner = repo_parts[0] if len(repo_parts) > 1 else None
            repo_name = repo_parts[1] if len(repo_parts) > 1 else repo
            result = await GitOpsHandler.get_content(
                tenant=tenant,
                owner=owner,
                repo=repo_name,
                file_path=f".github/workflows/{name}",
                branch=listing_branch,
            )
            return result.get("content") if result.get("exists") else None

        existing = await resolve_eks_workflow(
            file_names=names,
            env=env,
            service_name=service_label,
            canonical_name=canonical_name,
            fetch_content=_fetch,
            organization=organization,
        )
        if not existing:
            return canonical_path

        if existing != canonical_name:
            logger.info(
                f"{repo}@{base_branch}: reusing the existing EKS workflow "
                f"'{existing}' for {service_label} ({env}) — its inputs match "
                f"this service; not creating '{canonical_name}'"
            )
        return f".github/workflows/{existing}"

    @classmethod
    async def locate(cls, queue_item: Dict, workflow_context: PRWorkflowContext) -> FileLocationResponse:
        """
        Determine file location based on resource type.

        Args:
            queue_item: Dictionary containing either:
                1. TransactionQueueModel fields:
                    - infra_type: Infrastructure type (e.g., 's3', 'sqs', 'add_route')
                    - environment: Environment name
                    - tenant_code: Tenant code
                    - config_snapshot: JSONB with complete config (includes region, product_name, identifier, etc.)
                2. Direct API call fields:
                    - case_ref_code: Type of resource
                    - tenant: Tenant code
                    - environment: Environment name
                    - version_index: Version index
                    - region: AWS region
                    - product_name: Product/application name
                    - identifier: Resource identifier

        Note:
            GitHub repository is derived from INFRA_GITHUB_REPOSITORY environment variable
            Branch name is derived from environment using tenant-specific mapping logic
            (Aspora tenant: all environments → 'stage' branch)

        Returns:
            FileLocationResponse with file location details
        """
        # Support both resource_type (direct API) and infra_type (TransactionQueueModel)
        case_type = queue_item.get('case_ref_code')
        # Extract queue_code if available
        queue_code = queue_item.get('queue_code') or queue_item.get('code')

        # Check if config_snapshot exists (TransactionQueueModel pattern)
        config_snapshot = queue_item.get('config_snapshot', {})

        infrastructuretype_ref_code= config_snapshot.get('infrastructuretype_ref_code','')

        # DEBUG: Log the key values for ECS condition
        logger.info(f"FileLocator.locate() called:")
        logger.info(f"  case_type = '{case_type}'")
        logger.info(f"  infrastructuretype_ref_code = '{infrastructuretype_ref_code}'")
        logger.info(f"  ECS condition: case_type=='update_service' ({case_type == 'update_service'}) AND infra_ref=='ecs_ec2_infrastructuretype_ref' ({infrastructuretype_ref_code == 'ecs_ec2_infrastructuretype_ref'})")
        # Extract fields with fallback chain: direct field → config_snapshot → default
        tenant = queue_item.get('tenant') or queue_item.get('tenant_code', '')
        environment = queue_item.get('environment') or config_snapshot.get('environment') or config_snapshot.get('environment_enum', '')
        version_index = queue_item.get('version_index') or config_snapshot.get('version_index', getattr(settings, 'infra_version_index', '01'))
        region = queue_item.get('region') or config_snapshot.get('region') or config_snapshot.get('geo_loc_mst_code', '')
        # queue_item's 'geo_loc_mst_code' is in the chain for group-keyed gateway
        # rows: their snapshot deliberately carries no routing, and the deploy-time
        # enrichment (_apply_gateway_routing) writes the group's live region under
        # that key — without it geo_loc resolved empty and the path silently fell
        # back to the ap-south-1 default, sending e.g. London PRs to Mumbai.
        geo_loc = queue_item.get('geo-loc') or queue_item.get('geo_loc_mst_code') or config_snapshot.get('geo-loc') or config_snapshot.get('geo_loc_mst_code', '')
        product_name = queue_item.get('product_name') or config_snapshot.get('product_name', '')

        # DEBUG: Log product_name to trace where it's coming from
        logger.info(f"[DB_USER_MGMT_DEBUG] FileLocator - queue_item.product_name='{queue_item.get('product_name')}'")
        logger.info(f"[DB_USER_MGMT_DEBUG] FileLocator - config_snapshot.product_name='{config_snapshot.get('product_name')}'")
        logger.info(f"[DB_USER_MGMT_DEBUG] FileLocator - resolved product_name='{product_name}'")
        logger.info(f"[DB_USER_MGMT_DEBUG] FileLocator - config_snapshot keys: {list(config_snapshot.keys())}")

        if not product_name:
            raise ValueError(f"product_name is required but not found in queue_item or config_snapshot for case_type='{case_type}'")
        identifier = queue_item.get('identifier') or config_snapshot.get('identifier', '')

        # GitHub repository is derived from per-tenant config on tenants_mst
        from app.db.session import AsyncSessionLocal
        from app.utils.tenant_config import get_tenant_config
        from app.repository.geo_loc_mst_repository import GeoLocMstRepository
        async with AsyncSessionLocal() as _db:
            tenant_cfg = await get_tenant_config(tenant, _db)
            geo_loc_mst = await GeoLocMstRepository(_db).get_by_code(geo_loc) if geo_loc else None
        github_repository = tenant_cfg.github_infra_repository

        # Human-readable geo label for file names (e.g. "mumbai" not "region-aspora-mumbai").
        # Spaces in the name are replaced with hyphens so it is safe for file paths.
        geo_loc_name = (
            geo_loc_mst.name.lower().replace(" ", "-") if geo_loc_mst else geo_loc
        )

        # Branch is determined by tenant-specific logic (NOT from queue_item)
        # For Aspora tenant, all environments go to 'stage' branch
        branch_name = cls._get_branch_name()

        # # Check if this is a service config (has service_config_code) vs standalone infra
        # service_config_code = queue_item.get('service_config_code') or config_snapshot.get('service_config_code')
        # is_service_config = bool(service_config_code)

        # Sanitize names for file paths
        product_name_sanitized = cls._sanitize_identifier(product_name) if product_name else ''
        env_sanitized = cls._get_folder_env(environment)
        region_sanitized = cls._get_aws_region_from_geo_loc(geo_loc)
        identifier_sanitized = cls._sanitize_identifier(identifier) if identifier else ''

        # Extract service_name from config_snapshot if available (needed for ECS)
        service_name = config_snapshot.get('service_name', '')
        # Remove -service suffix if present before sanitizing for file paths
        if service_name.lower().endswith('-service'):
            service_name = service_name[:-8]  # Remove last 8 characters ("-service")
        service_name_sanitized = cls._sanitize_identifier(service_name) if service_name else ''

        # Determine file paths based on resource type (switch case pattern)
        files = []
        file_path = None  # Will be set by case-specific logic
        script_gen_key = None  # Overrides case_type for generator dispatch; set below where a case_type has more than one generator
        response = None  # Will be assigned at the end

        # ── Delete cases: post-action-only (DB soft-delete) ─────────────────
        # delete_* queue items make no file changes — they exist solely to drive
        # their post-action component (cascade soft-delete: variable_mst +
        # infra/service_config status → SOFT_DELETED). Return an empty file list
        # and early-return so we never fall through to the single-file block,
        # which would build a FileLocationItem with a None file_path and fail
        # validation.
        if case_type and case_type.startswith('delete_'):
            response = FileLocationResponse(files=[])
            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}
            return response

        # ECS Service Config - returns 3 files: terragrunt.hcl, pipeline, dockerfile
        # 'manage_variable' is the variable-tab redeploy vehicle: same file set
        # as a settings update (snapshot = last deployed config), different
        # case_ref so it never collides with a pending settings draft.
        if case_type in ('update_service', 'manage_variable') and infrastructuretype_ref_code=='ecs_ec2_infrastructuretype_ref':
            # Get services folder based on service type
            service_type = config_snapshot.get('service_type', 'API')
            if service_type == 'OPS_TOOLS':
                services_folder = 'ops-tools'
            else:  # API or default
                services_folder = 'services'

            # 1. Terragrunt HCL file (uses INFRA_GITHUB_REPOSITORY from env)
            # Service folder name should have -service suffix (e.g., rewards-api-service)
            service_folder_name = f"{service_name_sanitized}-service" if not service_name_sanitized.endswith("-service") else service_name_sanitized
            hcl_file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/{services_folder}/{service_folder_name}/terragrunt.hcl"

            # IMPORTANT: For Dockerfile and Pipeline, use repository from config_snapshot
            # NOT from INFRA_GITHUB_REPOSITORY environment variable
            # This is because Dockerfile and Pipeline are in the SERVICE repository,
            # while Terragrunt files are in the INFRA repository
            service_repository = config_snapshot.get('repository', '')

            # For branches, we need to handle multiple branches
            # The config_snapshot may have 'branches' or 'selected_branches'
            service_branches = config_snapshot.get('branches') or config_snapshot.get('selected_branches', [])

            # If no branches in config, fall back to single branch from env
            # if not service_branches:
            #     service_branches = [branch_name]  # Use the env-derived branch as fallback

            logger.info(f"Service config using repository: {service_repository}, branches: {service_branches}")

            # 2. Pipeline files - one per branch
            pipeline_files = []
            for svc_branch in service_branches:
                # Get or create feature branch from workflow context for this repo+branch combination
                # In preview mode, use base_branch instead of creating a feature branch
                if workflow_context.skip_commit:
                    feature_branch = svc_branch
                else:
                    feature_branch = await cls._get_or_create_feature_branch(
                        workflow_context=workflow_context,
                        repo=service_repository,
                        base_branch=svc_branch,
                        tenant=tenant,
                        pr_type="workflow",
                    )
                    if feature_branch is None:
                        continue

                pipeline_file_path = f".github/workflows/{service_name_sanitized}-{env_sanitized}.yml"
                pipeline_files.append(FileLocationItem(
                    repo=service_repository,
                    file_path=pipeline_file_path,
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "region": region,
                        "file_type": "pipeline"
                    },
                    queue_code=queue_code,
                    base_branch=svc_branch,
                    target_branch=svc_branch,
                    feature_branch=feature_branch,
                    script_gen_key="ecs_pipeline",
                    infra_type_ref=case_type
                ))

            # 3. Dockerfile (from config or default docker/{service}/Dockerfile)
            # Note: Dockerfile path is the SAME across all branches (it's the source file)
            dockerfile_path = config_snapshot.get('dockerfile_path')
            print(f"Path:{dockerfile_path}")
            if dockerfile_path:
                dockerfile_path = cls._sanitize_dockerfile_path(dockerfile_path)
            else:
                # Default: docker/{service}/Dockerfile if build_path exists, else Dockerfile
                # build_path = config_snapshot.get('build_path')
                # if build_path:
                #     dockerfile_path = f"docker/{service_name_sanitized}/Dockerfile"
                # else:
                dockerfile_path = "Dockerfile"

            # Create FileLocationItem for Dockerfile (one per branch since we might modify it per branch)
            dockerfile_files = []
            for svc_branch in service_branches:
                # Get or create feature branch from workflow context for this repo+branch combination
                # In preview mode, use base_branch instead of creating a feature branch
                if workflow_context.skip_commit:
                    feature_branch = svc_branch
                else:
                    feature_branch = await cls._get_or_create_feature_branch(
                        workflow_context=workflow_context,
                        repo=service_repository,
                        base_branch=svc_branch,
                        tenant=tenant,
                        pr_type="workflow",
                    )
                    if feature_branch is None:
                        continue

                dockerfile_files.append(FileLocationItem(
                    repo=service_repository,
                    file_path=dockerfile_path,
                    config={
                        "case_": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "region": region,
                        "file_type": "dockerfile"
                    },
                    queue_code=queue_code,
                    base_branch=svc_branch,
                    target_branch=svc_branch,
                    feature_branch=feature_branch,
                    script_gen_key="ecs_dockerfile",
                    infra_type_ref=case_type
                ))

            # Combine all files: terragrunt + pipelines (multi-branch) + dockerfiles (multi-branch)
            # Get or create feature branch for terragrunt HCL file
            # In preview mode, use base_branch instead of creating a feature branch
            if workflow_context.skip_commit:
                hcl_feature_branch = branch_name
            else:
                hcl_feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=github_repository,
                    base_branch=branch_name,
                    tenant=tenant,
                    pr_type="infrastructure",
                )

            # Create HCL FileLocationItem (skip if branch is None — conflict resolve for different repo)
            if hcl_feature_branch is not None:
                hcl_item = FileLocationItem(
                    repo=github_repository,
                    file_path=hcl_file_path,
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "region": region,
                        "file_type": "terragrunt"
                    },
                    queue_code=queue_code,
                    base_branch=branch_name,
                    target_branch=branch_name,
                    feature_branch=hcl_feature_branch,
                    script_gen_key="ecs_terragrunt",
                    infra_type_ref=case_type
                )
                logger.info(f"Created ECS terragrunt HCL FileLocationItem: repo={github_repository}, path={hcl_file_path}, script_gen_key=ecs_terragrunt")
                files = [hcl_item]
            logger.info(f"ECS files list initialized with HCL: {len(files)} files")

            # Add pipeline files (one per branch)
            files.extend(pipeline_files)
            logger.info(f"After adding pipelines: {len(files)} files")

            # Add dockerfile files (one per branch)
            files.extend(dockerfile_files)
            logger.info(f"After adding dockerfiles: {len(files)} files")

            # 4. Env config files (configs.json and secrets.json)
            # Skip env files for Falcon services (tenant-specific rule)
            service_lower = service_name.lower() if service_name else ""
            falcon_services = {
                "falcon-api",
                "falcon-consumer",
                "falcon-worker",
                "falcon-api-dev-service",
                "falcon-consumer-dev-service",
                "falcon-worker-dev-service",
            }
            skip_env_files = (
                product_name.lower() == "falcon"
                and service_lower in falcon_services
                and tenant.lower() in VANCE_ASPORA_TENANTS
            )

            if not skip_env_files:
                # Use helper methods for consistent path building
                env_for_path = cls._normalize_environment_for_env_files(environment, tenant)
                envs_folder = cls._get_envs_folder_name(environment, tenant)
                service_for_env = cls._get_service_name_for_env_files(service_name_sanitized)

                # OPS_TOOLS uses dev-tools shared folder, others use service-specific folder
                if service_type == "OPS_TOOLS":
                    env_subfolder = "dev-tools"
                else:
                    env_subfolder = service_for_env

                env_base_path = (
                    f"environment/{product_name_sanitized}-{env_for_path}-{version_index}/"
                    f"{region_sanitized}/{envs_folder}/{env_subfolder}"
                )
                configs_file_path = f"{env_base_path}/non-secure/{service_for_env}-configs.json"
                secrets_file_path = f"{env_base_path}/secure/{service_for_env}-secrets.json"

                # Get or create feature branch for env files (same as HCL)
                if workflow_context.skip_commit:
                    env_feature_branch = branch_name
                else:
                    env_feature_branch = await cls._get_or_create_feature_branch(
                        workflow_context=workflow_context,
                        repo=github_repository,
                        base_branch=branch_name,
                        tenant=tenant,
                        pr_type="infrastructure",
                    )

                if env_feature_branch is not None:
                    # Add configs.json
                    files.append(FileLocationItem(
                        repo=github_repository,
                        file_path=configs_file_path,
                        config={
                            "case_type": case_type,
                            "tenant": tenant,
                            "environment": environment,
                            "region": region,
                            "file_type": "env_configs"
                        },
                        queue_code=queue_code,
                        base_branch=branch_name,
                        target_branch=branch_name,
                        feature_branch=env_feature_branch,
                        script_gen_key="ecs_env_configs",
                        infra_type_ref=case_type
                    ))

                    # Add secrets.json
                    files.append(FileLocationItem(
                        repo=github_repository,
                        file_path=secrets_file_path,
                        config={
                            "case_type": case_type,
                            "tenant": tenant,
                            "environment": environment,
                            "region": region,
                            "file_type": "env_secrets"
                        },
                        queue_code=queue_code,
                        base_branch=branch_name,
                        target_branch=branch_name,
                        feature_branch=env_feature_branch,
                        script_gen_key="ecs_env_secrets",
                        infra_type_ref=case_type
                    ))
            else:
                logger.info(f"Skipping env files for Falcon service: {service_name}")

            # Set file_path for atlantis (will be used in shared atlantis logic below)
            file_path = hcl_file_path

            logger.info(f"ECS service config files for '{service_name}': TOTAL {len(files)} files")
            logger.info(f"  - Terragrunt: {hcl_file_path}")
            for pf in pipeline_files:
                logger.info(f"  - Pipeline: {pf.file_path}")
            for df in dockerfile_files:
                logger.info(f"  - Dockerfile: {df.file_path}")
            if not skip_env_files:
                logger.info(f"  - Configs: {configs_file_path}")
                logger.info(f"  - Secrets: {secrets_file_path}")

        # EKS Service Config - returns config.yaml, workflow YAML, and deployment.yaml
        elif case_type in ('update_service', 'manage_variable') and infrastructuretype_ref_code == 'eks_infrastructuretype_ref':
            # Resolve service_type from services_mst (services_mst_code is injected by
            # TransactionQueueService._enrich_config_snapshot for SERVICE_CONFIG rows)
            from app.repository.services_mst_repository import ServicesMstRepository
            _services_mst_code = config_snapshot.get("services_mst_code")
            service_type = "API"
            if _services_mst_code:
                async with AsyncSessionLocal() as _svc_db:
                    _svc = await ServicesMstRepository(_svc_db).get_by_code(_services_mst_code)
                    if _svc and _svc.service_type:
                        service_type = _svc.service_type.value.upper()
            is_worker = service_type == "BACKGROUND_SERVICE"
            logger.info(f"EKS service config: services_mst_code={_services_mst_code}, service_type={service_type}, is_worker={is_worker}")

            # Get service repository and branches from config_snapshot
            service_repository = config_snapshot.get('repository', '')
            service_branches = config_snapshot.get('branches') or config_snapshot.get('selected_branches', [])

            logger.info(f"EKS service config using repository: {service_repository}, branches: {service_branches}")

            # Determine file paths based on build_path
            build_path = (config_snapshot.get('build_path') or '').strip().strip('/')

            # Service repo holds workflow YAML + Dockerfile only.
            # The chart `values.yaml` (k8s-manifests repo) carries everything
            # the deploy needs — no separate `config-{svc}.yaml` is committed
            # to the service repo any more.

            # 1. Workflow YAML files - one per branch
            # Path: .github/workflows/deploy-{service_name}-{env}.yml
            workflow_files = []
            for svc_branch in service_branches:
                # Get or create feature branch from workflow context
                if workflow_context.skip_commit:
                    feature_branch = svc_branch
                else:
                    feature_branch = await cls._get_or_create_feature_branch(
                        workflow_context=workflow_context,
                        repo=service_repository,
                        base_branch=svc_branch,
                        tenant=tenant,
                        pr_type="workflow",
                    )
                    if feature_branch is None:
                        continue

                geo_loc_label = cls._sanitize_identifier(geo_loc_name) if geo_loc_name else region_sanitized
                svc_file_label = (
                    f"{service_name_sanitized}-service"
                    if not service_name_sanitized.endswith("-service")
                    else service_name_sanitized
                )
                # The name DevLift generates. A service whose workflow was
                # written by hand carries a different one — adopt that file
                # instead of committing a second workflow beside it.
                canonical_workflow_name = (
                    f"deploy-{svc_file_label}-eks-{env_sanitized}-{geo_loc_label}.yml"
                )
                workflow_file_path = await cls._resolve_eks_workflow_path(
                    tenant=tenant,
                    repo=service_repository,
                    feature_branch=feature_branch,
                    base_branch=svc_branch,
                    env=env_sanitized,
                    service_label=svc_file_label,
                    canonical_name=canonical_workflow_name,
                    # org_name is what the workflow's `organization:` input
                    # carries ("vance-core"); config_enrichment writes it onto
                    # the snapshot before the locator runs.
                    organization=(
                        config_snapshot.get("org_name")
                        or config_snapshot.get("organization")
                        or ""
                    ),
                )

                workflow_files.append(FileLocationItem(
                    repo=service_repository,
                    file_path=workflow_file_path,
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "region": region,
                        "file_type": "eks_workflow",
                        "infrastructure_type": "eks"
                    },
                    queue_code=queue_code,
                    base_branch=svc_branch,
                    target_branch=svc_branch,
                    feature_branch=feature_branch,
                    script_gen_key="eks_pipeline_workflow",
                    infra_type_ref=infrastructuretype_ref_code
                ))

            # 3. Dockerfile - one per branch (only when generate_dockerfile=True)
            # When generate_dockerfile=False the user owns their Dockerfile; don't touch it.
            dockerfile_files = []
            dockerfile_path = config_snapshot.get('dockerfile_path', '')
            generate_dockerfile = config_snapshot.get('generate_dockerfile', False)
            if dockerfile_path and generate_dockerfile:
                # Sanitize dockerfile path (remove leading/trailing slashes)
                dockerfile_path = cls._sanitize_dockerfile_path(dockerfile_path)

                for svc_branch in service_branches:
                    # Get or create feature branch from workflow context
                    if workflow_context.skip_commit:
                        feature_branch = svc_branch
                    else:
                        feature_branch = await cls._get_or_create_feature_branch(
                            workflow_context=workflow_context,
                            repo=service_repository,
                            base_branch=svc_branch,
                            tenant=tenant,
                            pr_type="workflow",
                        )
                        if feature_branch is None:
                            continue

                    dockerfile_files.append(FileLocationItem(
                        repo=service_repository,
                        file_path=dockerfile_path,
                        config={
                            "case_": case_type,
                            "tenant": tenant,
                            "environment": environment,
                            "region": region,
                            "file_type": "dockerfile"
                        },
                        queue_code=queue_code,
                        base_branch=svc_branch,
                        target_branch=svc_branch,
                        feature_branch=feature_branch,
                        script_gen_key="ecs_dockerfile",  # Same key as ECS - Docker is infrastructure-agnostic
                        infra_type_ref=case_type
                    ))

            # 4. EKS app terragrunt.hcl in the infra repo
            # Path: environment/{product}-{env}-{idx}/{region}/eks-workloads/{cluster_id}/services/{service}-service/terragrunt.hcl
            # cluster_id ("application" or "backend") is resolved from CLUSTER_DEPENDENCY_PATH
            # using infrastructure_mst_code / cluster_arn / cluster_name from config_snapshot.
            from app.plugin.aspora.script_gen_components.aspora_eks_terragrunt_script_gen_component import (
                CLUSTER_DEPENDENCY_PATH as _EKS_CLUSTER_MAP,
                DEFAULT_DEPENDENCY_PATH as _EKS_DEFAULT_DEP,
            )
            _cs = config_snapshot
            _dep_path = (
                _EKS_CLUSTER_MAP.get(_cs.get("cluster_arn", ""))
                or _EKS_CLUSTER_MAP.get(_cs.get("cluster_name") or _cs.get("ecs_cluster", ""))
                or _EKS_CLUSTER_MAP.get(_cs.get("infrastructure_mst_code", ""))
                or _EKS_DEFAULT_DEP
            )
            eks_cluster_id = _dep_path.split("/")[-1]  # "application" or "backend"
            k8s_cluster_folder = cls._resolve_k8s_cluster_folder(_cs)

            eks_service_folder_name = (
                f"{service_name_sanitized}-service"
                if not service_name_sanitized.endswith("-service")
                else service_name_sanitized
            )
            eks_hcl_file_path = (
                f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}"
                f"/{region_sanitized}/eks-workloads/{eks_cluster_id}/services/{eks_service_folder_name}/terragrunt.hcl"
            )

            if workflow_context.skip_commit:
                eks_hcl_feature_branch = branch_name
            else:
                eks_hcl_feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=github_repository,
                    base_branch=branch_name,
                    tenant=tenant,
                    pr_type="infrastructure",
                )

            eks_terragrunt_files = []
            if eks_hcl_feature_branch is not None:
                eks_terragrunt_files.append(FileLocationItem(
                    repo=github_repository,
                    file_path=eks_hcl_file_path,
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "region": region,
                        "file_type": "terragrunt",
                        "infrastructure_type": "eks",
                    },
                    queue_code=queue_code,
                    base_branch=branch_name,
                    target_branch=branch_name,
                    feature_branch=eks_hcl_feature_branch,
                    script_gen_key="eks_terragrunt",
                    infra_type_ref=infrastructuretype_ref_code,
                ))

            # 5. k8s-manifests repo (chart + env folder)
            # Repo + branch are read inline from the tenant JSONB with a fallback
            # so per-tenant overrides land here without touching TenantConfig.
            github_cfg = getattr(tenant_cfg, "_github", None) or {}
            # Per-tenant fallback when JSONB hasn't been backfilled yet.
            _k8s_fallback_repo = (
                "Regobs/k8s-manifests" if tenant == "vance" else "Vance-Club/k8s-manifests"
            )
            k8s_repository = (
                github_cfg.get("k8s_manifests_repository") or _k8s_fallback_repo
            )
            k8s_branch = github_cfg.get("k8s_manifests_branch") or "stage"

            if workflow_context.skip_commit:
                k8s_feature_branch = k8s_branch
            else:
                k8s_feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=k8s_repository,
                    base_branch=k8s_branch,
                    tenant=tenant,
                    pr_type="k8s_manifest",
                )

            k8s_manifest_files = []
            if k8s_feature_branch is not None:
                # Path conventions:
                #   chart:  charts/services/{svc}-service/...
                #   env:    environments/vance-{product}/{env}/{aws_region}/{svc}-service/...
                k8s_service_name = cls._get_service_name_for_env_files(service_name_sanitized)
                # k8s-manifests env folder uses "stage" not "staging" — handled
                # inside the script-gen component so the env path stays consistent.
                k8s_env = "stage" if env_sanitized == "stage" else env_sanitized
                k8s_aws_region = region_sanitized

                chart_dir = f"charts/services/{k8s_service_name}"
                env_dir = (
                    f"environments/vance-{product_name_sanitized}/{k8s_env}/{k8s_aws_region}"
                    f"/{k8s_cluster_folder}/services/{k8s_service_name}"
                )

                def _k8s_item(file_path: str, script_gen_key: str) -> FileLocationItem:
                    return FileLocationItem(
                        repo=k8s_repository,
                        file_path=file_path,
                        config={
                            "case_type": case_type,
                            "tenant": tenant,
                            "environment": environment,
                            "region": region,
                            "file_type": script_gen_key,
                            "infrastructure_type": "eks",
                            "service_type": service_type,
                        },
                        queue_code=queue_code,
                        base_branch=k8s_branch,
                        target_branch=k8s_branch,
                        feature_branch=k8s_feature_branch,
                        script_gen_key=script_gen_key,
                        infra_type_ref=infrastructuretype_ref_code,
                    )

                # Chart (skip-if-exists for static, always re-render for values.yaml)
                k8s_manifest_files.append(_k8s_item(f"{chart_dir}/Chart.yaml", "k8s_chart_meta"))
                k8s_manifest_files.append(_k8s_item(f"{chart_dir}/values.yaml", "k8s_chart_values"))

                _api_templates = (
                    "configmap.yaml",
                    "deployment.yaml",
                    "ingress.yaml",
                    "namespace.yaml",
                    "resources.yaml",
                    "service.yaml",
                    "serviceaccount.yaml",
                )
                _worker_templates = (
                    "configmap.yaml",
                    "deployment.yaml",
                    "namespace.yaml",
                    "resources.yaml",
                    "serviceaccount.yaml",
                )
                for tpl_basename in (_worker_templates if is_worker else _api_templates):
                    k8s_manifest_files.append(
                        _k8s_item(f"{chart_dir}/templates/{tpl_basename}", "k8s_chart_template")
                    )

                # Environment values (always re-render). The configs/.env
                # file is intentionally NOT committed — the env values.yaml
                # carries everything ArgoCD needs.
                k8s_manifest_files.append(_k8s_item(f"{env_dir}/values.yaml", "k8s_env_values"))

            # Combine all files: workflow + dockerfile + eks terragrunt + k8s-manifests
            files = (
                workflow_files
                + dockerfile_files
                + eks_terragrunt_files
                + k8s_manifest_files
            )

            logger.info(f"EKS service config files for '{service_name}':")
            for wf in workflow_files:
                logger.info(f"  - Workflow: {wf.file_path} (branch: {wf.base_branch})")
            for df in dockerfile_files:
                logger.info(f"  - Dockerfile: {df.file_path} (branch: {df.base_branch})")
            for tf in eks_terragrunt_files:
                logger.info(f"  - Terragrunt: {tf.file_path} (branch: {tf.base_branch})")
            for mf in k8s_manifest_files:
                logger.info(f"  - K8sManifests[{mf.script_gen_key}]: {mf.file_path}")

            # Expose hcl path so the shared atlantis logic below can append atlantis.yaml
            file_path = eks_hcl_file_path

        elif case_type == 'rollback_service':
            github_cfg = getattr(tenant_cfg, "_github", None) or {}
            _k8s_fallback_repo = (
                "Regobs/k8s-manifests" if tenant == "vance" else "Vance-Club/k8s-manifests"
            )
            k8s_repository = github_cfg.get("k8s_manifests_repository") or _k8s_fallback_repo
            k8s_branch = github_cfg.get("k8s_manifests_branch") or "stage"

            k8s_service_name = cls._get_service_name_for_env_files(service_name_sanitized)
            k8s_env = "stage" if env_sanitized == "stage" else env_sanitized
            k8s_aws_region = region_sanitized

            k8s_cluster_folder = cls._resolve_k8s_cluster_folder(config_snapshot)
            env_file_path = (
                f"environments/vance-{product_name_sanitized}/{k8s_env}/{k8s_aws_region}"
                f"/{k8s_cluster_folder}/services/{k8s_service_name}/values.yaml"
            )

            service_type = config_snapshot.get("service_type") or "API"
            files = [FileLocationItem(
                repo=k8s_repository,
                file_path=env_file_path,
                config={
                    "case_type": case_type,
                    "tenant": tenant,
                    "environment": environment,
                    "region": region,
                    "infrastructure_type": "eks",
                    "service_type": service_type,
                },
                queue_code=queue_code,
                base_branch=k8s_branch,
                target_branch=k8s_branch,
                feature_branch=k8s_branch,
                script_gen_key="k8s_rollback",
                infra_type_ref="eks_infrastructuretype_ref",
            )]

        elif case_type == 'add_route':
            # Gateway: environment/{product_name}-{env}-{version_index}/{region}/gateway/terragrunt.hcl
            file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/gateway/terragrunt.hcl"
            # EVERY add_route row goes to v2 — the Gateway tab's and the chat/MCP
            # flow's alike.
            #
            # v2 reads the snapshot shape itself and picks its own path: `groups`
            # present is the scope-keyed delta flow, absent is the flat one-route
            # snapshot the chat/MCP writer produces, which it reconciles from the
            # service's DB rows (`_apply_incremental`). So the discriminator that
            # used to live here now lives in the one place that acts on it, and
            # the two cannot disagree.
            #
            # Sending chat/MCP rows here is what makes route GROUPS and
            # regex_priority reachable from chat at all: v1 has no group concept
            # and never emits a route_config block, so a priority stored in the
            # DB could never reach terragrunt through it. Every chat route landed
            # in the service's default group at priority 0, and two groups
            # claiming one path at 0 have no tie-break — Kong's choice is
            # arbitrary.
            #
            # v1 (`add_route`) is left registered in ScriptGenHandler but is no
            # longer dispatched from here. It is dead, not deleted — removing it
            # is a separate cleanup.
            script_gen_key = 'add_route_v2'

        elif case_type in ['s3', 's3-bucket', 's3_bucket', 'create_bucket']:
            # S3 Bucket: environment/{product_name}-{env}-{version_index}/{region}/buckets/{identifier}/terragrunt.hcl
            file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/buckets/{identifier_sanitized}/terragrunt.hcl"

        elif case_type in ['sqs', 'sqs-queue', 'sqs_queue', 'create_queue']:
            # SQS Queue: environment/{product_name}-{env}-{version_index}/{region}/queues/{identifier}/terragrunt.hcl
            file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/queues/{identifier_sanitized}/terragrunt.hcl"

        elif case_type in ['dynamodb', 'dynamo', 'dynamodb-table', 'dynamodb_table', 'table_management']:
            # DynamoDB: environment/{product_name}-{env}-{version_index}/{region}/tables/{identifier}/terragrunt.hcl
            file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/dynamo/{identifier_sanitized}/terragrunt.hcl"

        elif case_type in ['mysql_user_management', 'postgresql_user_management', 'user_management']:
            # Database User Management: Multiple files - one per server
            # config_snapshot contains mysql_servers and pgsql_servers arrays
            # Each server gets its own file: environment/{product}-{env}-{version}/{region}/database/{db_server_name}/terragrunt.hcl

            # (product, env, server) combos that store MySQL users in a separate
            # mysql_users.hcl file instead of inline in terragrunt.hcl.
            SEPARATE_MYSQL_USERS_FILE_ENTRIES = {
                ("core", "stage", "common-mysql"),
            }

            # Process MySQL servers
            mysql_servers = config_snapshot.get('mysql_servers', [])
            for server in mysql_servers:
                db_server_name = server.get('db_server_name', '')
                db_server_name_sanitized = db_server_name if db_server_name else ''

                if db_server_name_sanitized:
                    use_separate_file = (
                        (product_name_sanitized, env_sanitized, db_server_name_sanitized)
                        in SEPARATE_MYSQL_USERS_FILE_ENTRIES
                    )

                    if use_separate_file:
                        server_file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/database/{db_server_name_sanitized}/mysql_users.hcl"
                    else:
                        server_file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/database/{db_server_name_sanitized}/terragrunt.hcl"

                    # Get or create feature branch from workflow context for this server
                    # In preview mode, use base_branch instead of creating a feature branch
                    if workflow_context.skip_commit:
                        feature_branch = branch_name
                    else:
                        feature_branch = await cls._get_or_create_feature_branch(
                            workflow_context=workflow_context,
                            repo=github_repository,
                            base_branch=branch_name,
                            tenant=tenant,
                            pr_type="infrastructure",
                        )
                        if feature_branch is None:
                            continue

                    files.append(FileLocationItem(
                        repo=github_repository,
                        file_path=server_file_path,
                        config={
                            "case_type": case_type,
                            "tenant": tenant,
                            "environment": environment,
                            "region": region,
                            "db_type": "mysql",
                            "db_server_name": db_server_name,
                            "file_type": "terragrunt",
                        },
                        queue_code=queue_code,
                        base_branch=branch_name,
                        target_branch=branch_name,
                        feature_branch=feature_branch,
                        script_gen_key=case_type,
                        infra_type_ref=case_type
                    ))

            # (product, env, server) combos that store users in a separate
            # psql_users.hcl file instead of inline in terragrunt.hcl.
            # Extend this set when rolling out the pattern to additional environments.
            SEPARATE_USERS_FILE_ENTRIES = {
                ("core", "stage", "common-pg"),
                ("core", "prod", "common-pg"),
            }

            # Process PostgreSQL servers
            pgsql_servers = config_snapshot.get('pgsql_servers', [])
            for server in pgsql_servers:
                db_server_name = server.get('db_server_name', '')
                db_server_name_sanitized = db_server_name if db_server_name else ''

                if db_server_name_sanitized:
                    use_separate_file = (
                        (product_name_sanitized, env_sanitized, db_server_name_sanitized)
                        in SEPARATE_USERS_FILE_ENTRIES
                    )

                    if use_separate_file:
                        server_file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/database/{db_server_name_sanitized}/psql_users.hcl"
                        server_script_gen_key = "user_management_separate_file"
                        server_file_type = "psql_users_file"
                    else:
                        server_file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/database/{db_server_name_sanitized}/terragrunt.hcl"
                        server_script_gen_key = case_type
                        server_file_type = "terragrunt"

                    # Get or create feature branch from workflow context for this server
                    # In preview mode, use base_branch instead of creating a feature branch
                    if workflow_context.skip_commit:
                        feature_branch = branch_name
                    else:
                        feature_branch = await cls._get_or_create_feature_branch(
                            workflow_context=workflow_context,
                            repo=github_repository,
                            base_branch=branch_name,
                            tenant=tenant,
                            pr_type="infrastructure",
                        )
                        if feature_branch is None:
                            continue

                    files.append(FileLocationItem(
                        repo=github_repository,
                        file_path=server_file_path,
                        config={
                            "case_type": case_type,
                            "tenant": tenant,
                            "environment": environment,
                            "region": region,
                            "db_type": "postgresql",
                            "db_server_name": db_server_name,
                            "file_type": server_file_type,
                        },
                        queue_code=queue_code,
                        base_branch=branch_name,
                        target_branch=branch_name,
                        feature_branch=feature_branch,
                        script_gen_key=server_script_gen_key,
                        infra_type_ref=server_script_gen_key
                    ))

            # If no servers found, log warning but continue
            if not files:
                logger.warning(f"No MySQL or PostgreSQL servers found in config_snapshot for user_management resource_type")

            logger.info(f"User management files generated: {len(files)} files ({len(mysql_servers)} MySQL, {len(pgsql_servers)} PostgreSQL)")

            response = FileLocationResponse(files=files)

        elif case_type == 'database_creation':
            # Database Creation: environment/{product_name}-{env}-{version_index}/{region}/database/{identifier}/terragrunt.hcl
            db_server_name=config_snapshot.get('db_server_name', '')
            file_path = f"environment/{product_name_sanitized}-{env_sanitized}-{version_index}/{region_sanitized}/database/{db_server_name}/terragrunt.hcl"

        # Log file path if set (not set for user_management which handles its own files)
        if file_path:
            logger.info(f"File location determined for resource_type '{case_type}': {file_path}")

        # Get or create feature branch from workflow context for standalone infra
        # In preview mode, use base_branch instead of creating a feature branch
        if workflow_context.skip_commit:
            feature_branch = branch_name
        else:
            feature_branch = await cls._get_or_create_feature_branch(
                workflow_context=workflow_context,
                repo=github_repository,
                base_branch=branch_name,
                tenant=tenant,
                pr_type="infrastructure",
            )

        # Only create single-file response for case types that don't create multiple files
        # ecs, ecs_ec2, user_management, create_service, and update_service already create their own responses above
        multi_file_case_types = ['ecs_ec2', 'ecs', 'mysql_user_management', 'postgresql_user_management', 'user_management', 'create_service', 'update_service', 'rollback_service']
        if case_type not in multi_file_case_types and feature_branch is not None:
            files = [
                FileLocationItem(
                    repo=github_repository,
                    file_path=file_path,
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "region": region,
                    },
                    queue_code=queue_code,
                    base_branch=branch_name,
                    target_branch=branch_name,
                    feature_branch=feature_branch,
                    script_gen_key=script_gen_key or case_type,
                    infra_type_ref=case_type
                )
            ]

        # 'add_route' is here to RESOLVE the gateway's Atlantis project name, not to
        # add an entry: the gateway project already exists in atlantis.yaml, so the
        # component finds it and returns the content unchanged. Without this item the
        # deploy has no atlantis_project_name and falls back to rebuilding one from a
        # string formula, which omits the region and reads a group-keyed row's
        # config_snapshot — a delta that carries no product or environment at all.
        atlantis_case_types = {
            's3', 's3-bucket', 's3_bucket', 'create_bucket',
            'sqs', 'sqs-queue', 'sqs_queue', 'create_queue',
            'dynamodb', 'dynamo', 'dynamodb-table', 'dynamodb_table', 'table_management','update_service','create_service',
            'add_route',
        }
        if case_type in atlantis_case_types and file_path and feature_branch is not None:
            # For ECS cases (update_service with ecs_ec2), use infrastructuretype_ref_code
            # so atlantis script gen routes to ECS entry builder
            atlantis_infra_type_ref = infrastructuretype_ref_code if infrastructuretype_ref_code else case_type
            files.append(
                FileLocationItem(
                    repo=github_repository,
                    file_path="atlantis.yaml",
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "region": region,
                        "hcl_file_path": file_path,
                    },
                    queue_code=queue_code,
                    base_branch=branch_name,
                    target_branch=branch_name,
                    feature_branch=feature_branch,
                    script_gen_key="atlantis",
                    infra_type_ref=atlantis_infra_type_ref
                )
            )

        # Set response if not already set (ECS and user_management set it earlier)
        if response is None:
            response = FileLocationResponse(files=files)

        # Add response to workflow context
        queue_id = queue_item.get('id')
        if queue_id:
            workflow_context.file_location_responses[queue_id] = response

        return response

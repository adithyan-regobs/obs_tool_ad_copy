"""
Default File Locator

Generic EKS service onboarding file locator for tenants without a
tenant-specific locator implementation.

Handles case_type='update_service' + infrastructuretype_ref_code='eks_infrastructuretype_ref':
  - deployment/{service_name}-deployment.yaml  → combined K8s manifest (namespace + deployment + service + ingress)
  - .github/workflows/deploy-{service_name}-{env}.yml  → GitHub Actions CI/CD pipeline

Files land in the SERVICE repository (not an infra repo).
The deployment/ folder is created by the commit if it does not yet exist.
"""

import logging
import os
import re
import uuid
from typing import Any, Dict, Optional

from app.schemas.file_location_response_schema import FileLocationResponse, FileLocationItem
from app.schemas.pr_workflow_context import PRWorkflowContext
from app.utils.timing import log_timing

logger = logging.getLogger(__name__)


class DefaultFileLocator:

    @staticmethod
    async def _resolve_infra_repo(tenant: str) -> tuple:
        """Resolve tenant's infra repo and branch from tenant config DB."""
        from app.db.session import AsyncSessionLocal
        from app.utils.tenant_config import get_tenant_config
        async with AsyncSessionLocal() as _db:
            tenant_cfg = await get_tenant_config(tenant, _db)
        repo = tenant_cfg.github_infra_repository
        branch = tenant_cfg.github_infra_branch
        if branch == "main":
            branch = "stage"
        if not repo:
            raise ValueError(
                f"No infra repository configured for tenant '{tenant}'. "
                "Run provision_infrastructure first."
            )
        return (repo, branch)

    @staticmethod
    def _sanitize_identifier(identifier: str) -> str:
        """Sanitize identifier for file paths (spaces/underscores/hyphens → single hyphen)."""
        return re.sub(r'[\s_-]+', '-', identifier).strip('-').lower()

    @staticmethod
    def _denest_base_slug(base_branch_slug: str, repo_slug: str) -> str:
        """A generated branch slugifies to 'feature-infra-{repo}-{core}-{token}'.
        When one is reused as a base branch, embedding it whole would grow every
        rerun's name by a full copy of the previous one, until git's 255-byte
        cap and the DB columns overflow. Peel the generated layers so only the
        original core is ever embedded. Name-only: the real base branch used
        for git is untouched."""
        token_re = re.compile(r"-[0-9a-f]{8}$")
        while base_branch_slug.startswith("feature-infra-"):
            base_branch_slug = base_branch_slug[len("feature-infra-"):]
            if repo_slug and base_branch_slug.startswith(f"{repo_slug}-"):
                base_branch_slug = base_branch_slug[len(repo_slug) + 1:]
            base_branch_slug = token_re.sub("", base_branch_slug)
        return base_branch_slug

    @classmethod
    def _role_policy_item(
        cls,
        *,
        tenant: str,
        infra_repo: str,
        infra_branch: str,
        feature_branch: Optional[str],
        queue_code: Optional[str],
        case_type: str,
        environment: str,
        policy_kind: str,
        resource_arns,
        policy_op: str = "add",
    ) -> FileLocationItem:
        """Build the FileLocationItem that mutates the tenant's default-role
        ``custom_policy_json`` alongside a resource creation. ``resource_arns``
        may be a single ARN string or a list (for kinds like S3 that need
        both bucket and object ARNs in one statement). Ships on the same
        feature branch as the resource → single PR, single commit."""
        from app.core.config import settings as _s
        arns = [resource_arns] if isinstance(resource_arns, str) else list(resource_arns)
        return FileLocationItem(
            repo=infra_repo,
            file_path=(
                f"environment/{tenant}/{_s.onboarding_default_index}"
                f"/aws/{_s.onboarding_default_region}"
                f"/application/default/terragrunt.hcl"
            ),
            config={
                "case_type": case_type,
                "tenant": tenant,
                "environment": environment,
                "policy_kind": policy_kind,
                "resource_arns": arns,
                "policy_op": policy_op,
            },
            queue_code=queue_code,
            base_branch=infra_branch,
            target_branch=infra_branch,
            feature_branch=feature_branch,
            script_gen_key="default_aws_role",
            infra_type_ref=case_type,
        )

    @classmethod
    async def _build_delete_iam_revoke_item(
        cls,
        *,
        tenant: str,
        case_type: str,
        environment: str,
        queue_code: Optional[str],
        config_snapshot: Dict[str, Any],
        workflow_context: PRWorkflowContext,
    ) -> Optional[FileLocationItem]:
        """For ``delete_*`` case_refs on AWS-IAM-grant-bearing resources, build
        a ``_role_policy_item(policy_op="remove")`` keyed off the existing
        infrastructure_mst row.

        Reads (kind, ARNs) from ``infra.locator`` (populated by the resource's
        factory at create time). Returns ``None`` when the infra row is
        missing or the locator doesn't have the fields needed to derive ARNs
        — caller should treat that as "no IAM file to commit" and let the
        post-action handle the DB-only soft-delete.
        """
        infrastructure_mst_code = config_snapshot.get('infrastructure_mst_code', '')
        if not infrastructure_mst_code:
            logger.warning(
                "delete %s: missing infrastructure_mst_code in config_snapshot — skipping IAM revoke",
                case_type,
            )
            return None

        from app.db.session import AsyncSessionLocal
        from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
        async with AsyncSessionLocal() as _db:
            infra_repo_db = InfrastructureMstRepository(_db)
            infra = await infra_repo_db.get_by_code(infrastructure_mst_code)
        if not infra:
            logger.warning(
                "delete %s: infra row not found for code=%s — skipping IAM revoke",
                case_type, infrastructure_mst_code,
            )
            return None

        locator = infra.locator or {}
        from app.core.config import settings as _settings
        # IMPORTANT: every ARN is re-derived from the resolved AWS *name* +
        # `cloudRegion` (frontend-set, reliable) + settings.onboarding_default_account_id.
        # The locator's pre-stored `region`, `accountId`, `bucket_arn`,
        # `queue_arn`, `dlq_arn` fields are NOT trustworthy for old rows —
        # there's a frontend bug where the factory was given a different
        # region than the IAM policy file (which uses settings defaults),
        # and `accountId` lands as a placeholder ("000000000000") instead
        # of the real account. Names + cloudRegion are correct; everything
        # else we compute fresh so it matches what create-time wrote into
        # the role policy.
        account_id = _settings.onboarding_default_account_id
        region = locator.get('cloudRegion') or _settings.onboarding_default_region

        policy_kind: str = ""
        resource_arns: list = []

        if case_type == 'delete_bucket':
            bucket_name = locator.get('bucket_name', '')
            if not bucket_name:
                return None
            # S3 ARN is region-less; compute from bucket_name only.
            bucket_arn = f"arn:aws:s3:::{bucket_name}"
            policy_kind = "s3-rw"
            resource_arns = [bucket_arn, f"{bucket_arn}/*"]
        elif case_type == 'delete_queue':
            queue_name = locator.get('queue_name', '')
            if not (queue_name and account_id and region):
                return None
            queue_arn = f"arn:aws:sqs:{region}:{account_id}:{queue_name}"
            policy_kind = "sqs-rw"
            resource_arns = [queue_arn]
            dlq_name = locator.get('dlq_name')
            if dlq_name:
                resource_arns.append(
                    f"arn:aws:sqs:{region}:{account_id}:{dlq_name}"
                )
        elif case_type == 'delete_dynamodb_table':
            table_name = locator.get('table_name', '')
            if not (table_name and account_id and region):
                return None
            table_arn = f"arn:aws:dynamodb:{region}:{account_id}:table/{table_name}"
            policy_kind = "dynamodb-rw"
            resource_arns = [table_arn]
        elif case_type == 'delete_redis':
            cluster_name = locator.get('redis_cluster_name', '')
            identifier = locator.get('identifier', '')
            if not (cluster_name and identifier and account_id and region):
                return None
            identifier_sanitized = cls._sanitize_identifier(identifier)
            replication_group_arn = (
                f"arn:aws:elasticache:{region}:{account_id}:replicationgroup:{cluster_name}"
            )
            user_arn = (
                f"arn:aws:elasticache:{region}:{account_id}:user:{identifier_sanitized}-iam"
            )
            policy_kind = "redis-connect"
            resource_arns = [replication_group_arn, user_arn]
        elif case_type == 'delete_server':
            db_cluster_resource_id = locator.get('db_cluster_resource_id', '')
            if not (db_cluster_resource_id and account_id and region):
                logger.warning(
                    "delete_server: db_cluster_resource_id not in locator for %s — skipping IAM revoke",
                    infrastructure_mst_code,
                )
                return None
            aurora_iam_arn = (
                f"arn:aws:rds-db:{region}:{account_id}"
                f":dbuser:{db_cluster_resource_id}/iam_user"
            )
            policy_kind = "rds-aurora-connect"
            resource_arns = [aurora_iam_arn]
        else:
            return None

        infra_repo, infra_branch = await cls._resolve_infra_repo(tenant)
        if workflow_context.skip_commit:
            feature_branch = infra_branch
        else:
            feature_branch = await cls._get_or_create_feature_branch(
                workflow_context=workflow_context,
                repo=infra_repo,
                base_branch=infra_branch,
                tenant=tenant,
                pr_type="infrastructure",
            )

        return cls._role_policy_item(
            tenant=tenant,
            infra_repo=infra_repo,
            infra_branch=infra_branch,
            feature_branch=feature_branch,
            queue_code=queue_code,
            case_type=case_type,
            environment=environment,
            policy_kind=policy_kind,
            resource_arns=resource_arns,
            policy_op="remove",
        )

    @staticmethod
    def _get_folder_env(environment: str) -> str:
        env_map = {
            'dev': 'dev', 'development': 'dev',
            'staging': 'stage', 'stage': 'stage', 'stg': 'stage',
            'qa': 'qa', 'prod': 'prod', 'production': 'prod',
        }
        return env_map.get(environment.lower(), environment.lower())

    @staticmethod
    async def _get_or_create_feature_branch(
        workflow_context: PRWorkflowContext,
        repo: str,
        base_branch: str,
        tenant: str,
        pr_type: str = "infrastructure",
    ) -> Optional[str]:
        """
        Get or create a feature branch for the given repo + base_branch combination.
        Returns None when in conflict_resolve mode for a non-target repo.
        """
        from app.handlers.gitops_handler import GitOpsHandler

        _TYPE_PRIORITY = {"infrastructure": 3, "k8s_manifest": 2, "workflow": 1}
        key = f"{repo}|||{base_branch}"

        if key not in workflow_context.feature_branches:
            if workflow_context.is_conflict_resolve:
                logger.info(f"Conflict resolve: skipping non-target repo/branch {key}")
                return None

            repo_slug = DefaultFileLocator._sanitize_identifier(repo.replace("/", "-"))
            base_branch_slug = DefaultFileLocator._sanitize_identifier(base_branch.replace("/", "-"))
            base_branch_slug = DefaultFileLocator._denest_base_slug(base_branch_slug, repo_slug)
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
            feature_branch = f"{prefix}{body}-{token}"
            workflow_context.feature_branches[key] = {"branch": feature_branch, "type": pr_type}

            repo_parts = repo.split('/')
            owner = repo_parts[0] if len(repo_parts) > 1 else None
            repo_name = repo_parts[1] if len(repo_parts) > 1 else repo

            from app.db.session import AsyncSessionLocal as _ASL
            async with _ASL() as _db:
                component = GitOpsHandler.get_component(tenant, _db)
                timing_ctx = f"repo={repo_name} base={base_branch} feature={feature_branch}"
                with log_timing(logger, "feature_branch_create", context=timing_ctx):
                    branch_result = await component.create_branch(
                        owner=owner,
                        repo=repo_name,
                        base_branch=base_branch,
                        feature_branch=feature_branch,
                    )
            logger.info(f"Feature branch creation result: {branch_result}")
        else:
            fb_data = workflow_context.feature_branches[key]
            feature_branch = fb_data["branch"] if isinstance(fb_data, dict) else fb_data
            if isinstance(fb_data, dict):
                if _TYPE_PRIORITY.get(pr_type, 0) > _TYPE_PRIORITY.get(fb_data.get("type", "infrastructure"), 0):
                    fb_data["type"] = pr_type
            logger.debug(f"Reusing existing feature branch for {key}: {feature_branch}")

        return feature_branch

    @classmethod
    async def locate(cls, queue_item: Dict, workflow_context: PRWorkflowContext) -> FileLocationResponse:
        """
        Determine file locations for EKS service onboarding.

        Expects queue_item.config_snapshot to contain:
            - service_name           : Service name
            - repository             : Service GitHub repository (owner/repo)
            - branches               : List of branches to generate files for
            - environment            : Target environment
            - infrastructure_mst_code: FK to infrastructure_mst (used by script gen)
            - infrastructuretype_ref_code: Must be 'eks_infrastructuretype_ref'

        Returns:
            FileLocationResponse with deployment manifest + workflow file per branch
        """
        case_type = queue_item.get('case_ref_code')
        queue_code = queue_item.get('queue_code') or queue_item.get('code')
        config_snapshot = queue_item.get('config_snapshot', {})
        infrastructuretype_ref_code = config_snapshot.get('infrastructuretype_ref_code', '')
        tenant = queue_item.get('tenant') or queue_item.get('tenant_code', '')
        environment = (
            queue_item.get('environment')
            or config_snapshot.get('environment')
            or config_snapshot.get('environment_enum', '')
        )

        if case_type == 'update_service' and infrastructuretype_ref_code == 'eks_infrastructuretype_ref':
            service_name = config_snapshot.get('service_name', '')
            # Strip -service suffix before sanitising (consistent with rest of codebase)
            if service_name.lower().endswith('-service'):
                service_name = service_name[:-8]

            service_name_sanitized = cls._sanitize_identifier(service_name) if service_name else ''
            env_sanitized = cls._get_folder_env(environment)

            # Model serving uses vLLM image directly — no repository, Dockerfile, or build step
            is_model_serving = config_snapshot.get("service_type") == "MODEL_SERVING"

            nested_config = config_snapshot.get('config') or {}
            service_repository = (
                config_snapshot.get('repository')
                or nested_config.get('repository', '')
            )
            service_branches = (
                config_snapshot.get('branches')
                or config_snapshot.get('selected_branches')
                or nested_config.get('branches')
                or nested_config.get('selected_branches', [])
            )

            if not service_repository and not is_model_serving:
                logger.warning(
                    "config_snapshot has no 'repository' field. Keys present: %s",
                    list(config_snapshot.keys()),
                )
                raise ValueError(
                    "config_snapshot.repository is required for default EKS service onboarding"
                )
            if not service_name_sanitized:
                raise ValueError(
                    "config_snapshot.service_name is required for default EKS service onboarding"
                )

            generate_dockerfile = config_snapshot.get('generate_dockerfile', False)
            dockerfile_path = config_snapshot.get('dockerfile_path') or 'Dockerfile'
            dockerfile_path = dockerfile_path.strip('/')

            deployment_files = []
            workflow_files = []
            dockerfile_files = []

            # Model serving always uses Jenkins (no source code to build via GitHub Actions)
            ci_provider = "jenkins" if is_model_serving else config_snapshot.get("ci_provider", "github_actions")

            common_config = {
                "case_type": case_type,
                "tenant": tenant,
                "environment": environment,
                "infrastructure_type": "eks",
            }

            # Jenkins: inline deploy (mode="jenkins") + commit Jenkinsfile to infra repo
            if ci_provider == "jenkins":
                infra_repo, infra_branch = await cls._resolve_infra_repo(tenant)

                logger.info(
                    "ci_provider=jenkins for '%s' (%s) — routing to infra repo %s",
                    service_name, environment, infra_repo,
                )

                if workflow_context.skip_commit:
                    feature_branch = infra_branch
                else:
                    feature_branch = await cls._get_or_create_feature_branch(
                        workflow_context=workflow_context,
                        repo=infra_repo,
                        base_branch=infra_branch,
                        tenant=tenant,
                        pr_type="infrastructure",
                    )

                # Route model-serving to its own generator (no build stage, GPU resources)
                service_type = config_snapshot.get("service_type", "")
                jenkins_gen_key = (
                    "model_serving_jenkins_pipeline"
                    if service_type == "MODEL_SERVING"
                    else "eks_jenkins_pipeline"
                )

                deployment_files.append(FileLocationItem(
                    repo=infra_repo,
                    file_path=f"eks-services/{service_name_sanitized}/Jenkinsfile",
                    config={**common_config},
                    queue_code=queue_code,
                    base_branch=infra_branch,
                    target_branch=infra_branch,
                    feature_branch=feature_branch,
                    script_gen_key=jenkins_gen_key,
                    infra_type_ref=infrastructuretype_ref_code,
                    mode="jenkins",
                ))

                # K8s manifest files — committed to infra repo for version control
                manifest_gen_key = (
                    "model_serving_eks_deployment"
                    if is_model_serving
                    else "default_eks_deployment"
                )
                if is_model_serving:
                    k8s_types = [
                        ("00-namespace", "namespace"),
                        ("01-serviceaccount", "serviceaccount"),
                        ("02-efs-pvc-tenant", "efs-pvc-tenant"),
                        ("inference-service", "inference-service"),
                        ("07-secret-provider-class", "secret-provider-class"),
                        ("08-model-download-sa", "model-download-sa"),
                        ("09-model-download-spc", "model-download-spc"),
                        ("10-model-download-pvc", "model-download-pvc"),
                        ("model-download-job", "model-download-job"),
                    ]
                else:
                    # IngressClass + IngressClassParams (alb-public) are cluster-scoped
                    # and created once at tenant bootstrap (eks_bootstrap.build_ingress_class_yaml).
                    # Per-service Ingress wires into the shared ALB via ingressClassName: alb-public.
                    k8s_types = [
                        ("00-namespace", "namespace"),
                        ("01-serviceaccount", "serviceaccount"),
                        ("02-deployment", "deployment"),
                        ("03-service", "service"),
                        ("06-ingress", "ingress"),
                        ("07-secret-provider-class", "secret-provider-class"),
                    ]
                for file_name, k8s_type in k8s_types:
                    deployment_files.append(FileLocationItem(
                        repo=infra_repo,
                        file_path=f"eks-services/{service_name_sanitized}/deployment/{file_name}.yaml",
                        config={**common_config, "file_type": k8s_type},
                        queue_code=queue_code,
                        base_branch=infra_branch,
                        target_branch=infra_branch,
                        feature_branch=feature_branch,
                        script_gen_key=manifest_gen_key,
                        infra_type_ref=infrastructuretype_ref_code,
                    ))
            else:
                for svc_branch in service_branches:
                    if workflow_context.skip_commit:
                        feature_branch = svc_branch
                    else:
                        feature_branch = await cls._get_or_create_feature_branch(
                            workflow_context=workflow_context,
                            repo=service_repository,
                            base_branch=svc_branch,
                            tenant=tenant,
                            pr_type="k8s_manifest",
                        )
                        if feature_branch is None:
                            continue

                    # 1. K8s manifests → separate files in deployment/
                    # IngressClass + IngressClassParams are cluster-scoped and
                    # created once at tenant bootstrap; per-service Ingress joins
                    # the shared ALB via ingressClassName: alb-public.
                    k8s_types = [
                        ("00-namespace", "namespace"),
                        ("01-serviceaccount", "serviceaccount"),
                        ("02-deployment", "deployment"),
                        ("03-service", "service"),
                        ("06-ingress", "ingress"),
                    ]
                    for file_name, k8s_type in k8s_types:
                        deployment_files.append(FileLocationItem(
                            repo=service_repository,
                            file_path=f"deployment/{file_name}.yaml",
                            config={**common_config, "file_type": k8s_type},
                            queue_code=queue_code,
                            base_branch=svc_branch,
                            target_branch=svc_branch,
                            feature_branch=feature_branch,
                            script_gen_key="default_eks_deployment",
                            infra_type_ref=infrastructuretype_ref_code,
                        ))

                    # 2. GitHub Actions workflow → .github/workflows/deploy-{service}-{env}.yml
                    workflow_files.append(FileLocationItem(
                        repo=service_repository,
                        file_path=f".github/workflows/deploy-{service_name_sanitized}-{env_sanitized}.yml",
                        config={**common_config, "file_type": "eks_workflow"},
                        queue_code=queue_code,
                        base_branch=svc_branch,
                        target_branch=svc_branch,
                        feature_branch=feature_branch,
                        script_gen_key="default_eks_workflow",
                        infra_type_ref=infrastructuretype_ref_code,
                    ))

                    # 3. Dockerfile (only when user opts in via generate_dockerfile=True)
                    if generate_dockerfile:
                        dockerfile_files.append(FileLocationItem(
                            repo=service_repository,
                            file_path=dockerfile_path,
                            config={**common_config, "file_type": "dockerfile"},
                            queue_code=queue_code,
                            base_branch=svc_branch,
                            target_branch=svc_branch,
                            feature_branch=feature_branch,
                            script_gen_key="default_dockerfile",
                            infra_type_ref=infrastructuretype_ref_code,
                        ))

            files = deployment_files + workflow_files + dockerfile_files

            logger.info(f"Default EKS onboarding files for '{service_name}' ({environment}):")
            for df in deployment_files:
                logger.info(f"  - K8s [{df.config.get('file_type')}]: {df.file_path} (branch: {df.base_branch})")
            for wf in workflow_files:
                logger.info(f"  - Workflow    : {wf.file_path} (branch: {wf.base_branch})")
            for ddf in dockerfile_files:
                logger.info(f"  - Dockerfile  : {ddf.file_path} (branch: {ddf.base_branch})")

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        if case_type == 'update_service' and infrastructuretype_ref_code in (
            'ecs_infrastructuretype_ref', 'ecs_ec2_infrastructuretype_ref',
        ):
            # ── ECS service onboarding ──
            # Only a GitHub Actions workflow is needed (no K8s manifests).
            service_name = config_snapshot.get('service_name', '')
            if service_name.lower().endswith('-service'):
                service_name = service_name[:-8]

            service_name_sanitized = cls._sanitize_identifier(service_name) if service_name else ''
            env_sanitized = cls._get_folder_env(environment)

            nested_config = config_snapshot.get('config') or {}
            service_repository = (
                config_snapshot.get('repository')
                or nested_config.get('repository', '')
            )
            service_branches = (
                config_snapshot.get('branches')
                or config_snapshot.get('selected_branches')
                or nested_config.get('branches')
                or nested_config.get('selected_branches', [])
            )

            if not service_repository:
                raise ValueError(
                    "config_snapshot.repository is required for default ECS service onboarding"
                )
            if not service_name_sanitized:
                raise ValueError(
                    "config_snapshot.service_name is required for default ECS service onboarding"
                )

            generate_dockerfile = config_snapshot.get('generate_dockerfile', False)
            dockerfile_path = config_snapshot.get('dockerfile_path') or 'Dockerfile'
            dockerfile_path = dockerfile_path.strip('/')

            workflow_files = []
            dockerfile_files = []

            common_config = {
                "case_type": case_type,
                "tenant": tenant,
                "environment": environment,
                "infrastructure_type": "ecs",
            }

            for svc_branch in service_branches:
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

                workflow_files.append(FileLocationItem(
                    repo=service_repository,
                    file_path=f".github/workflows/deploy-{service_name_sanitized}-{env_sanitized}.yml",
                    config={**common_config, "file_type": "ecs_workflow"},
                    queue_code=queue_code,
                    base_branch=svc_branch,
                    target_branch=svc_branch,
                    feature_branch=feature_branch,
                    script_gen_key="default_ecs_workflow",
                    infra_type_ref=infrastructuretype_ref_code,
                ))

                if generate_dockerfile:
                    dockerfile_files.append(FileLocationItem(
                        repo=service_repository,
                        file_path=dockerfile_path,
                        config={**common_config, "file_type": "dockerfile"},
                        queue_code=queue_code,
                        base_branch=svc_branch,
                        target_branch=svc_branch,
                        feature_branch=feature_branch,
                        script_gen_key="default_dockerfile",
                        infra_type_ref=infrastructuretype_ref_code,
                    ))

            files = workflow_files + dockerfile_files

            logger.info(f"Default ECS onboarding files for '{service_name}' ({environment}):")
            for wf in workflow_files:
                logger.info(f"  - Workflow    : {wf.file_path} (branch: {wf.base_branch})")
            for ddf in dockerfile_files:
                logger.info(f"  - Dockerfile  : {ddf.file_path} (branch: {ddf.base_branch})")

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        if case_type == 'k8s_postgres_create_server':
            # Kubernetes Helm provisioning — inline Jenkins deploy + commit to infra repo
            server_name = config_snapshot.get('server_name', '')
            helm_chart_type = config_snapshot.get('helm_chart_type') or 'postgres'
            config_snapshot['helm_chart_type'] = helm_chart_type

            _HELMS_BASE = os.path.join(
                os.path.dirname(__file__), "..", "..", "..", "templates", "helms"
            )

            infra_repo, infra_branch = await cls._resolve_infra_repo(tenant)
            server_name_sanitized = cls._sanitize_identifier(server_name) if server_name else helm_chart_type

            if workflow_context.skip_commit:
                feature_branch = infra_branch
            else:
                feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=infra_repo,
                    base_branch=infra_branch,
                    tenant=tenant,
                    pr_type="infrastructure",
                )

            helm_base = f"helm/{server_name_sanitized}"

            files = [
                # Jenkinsfile — mode="jenkins" for inline deploy + git commit
                FileLocationItem(
                    repo=infra_repo,
                    file_path=f"{helm_base}/Jenkinsfile",
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "server_name": server_name,
                    },
                    queue_code=queue_code,
                    base_branch=infra_branch,
                    target_branch=infra_branch,
                    feature_branch=feature_branch,
                    script_gen_key="k8s_postgres_create_server",
                    infra_type_ref=case_type,
                    mode="jenkins",
                    template_path=os.path.abspath(
                        os.path.join(_HELMS_BASE, helm_chart_type, "values.yaml")
                    ),
                ),
                # values.yaml — git commit only (no inline deploy)
                FileLocationItem(
                    repo=infra_repo,
                    file_path=f"{helm_base}/values.yaml",
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "server_name": server_name,
                    },
                    queue_code=queue_code,
                    base_branch=infra_branch,
                    target_branch=infra_branch,
                    feature_branch=feature_branch,
                    script_gen_key="k8s_postgres_create_server",
                    infra_type_ref=case_type,
                    template_path=os.path.abspath(
                        os.path.join(_HELMS_BASE, helm_chart_type, "values.yaml")
                    ),
                ),
            ]

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        _HELM_JOBS_BASE = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "templates", "helm-jobs"
        )

        if case_type in ('k8s_postgres_create_database', 'k8s_postgres_create_user'):
            files = [FileLocationItem(
                repo="",
                file_path="",
                config={
                    "case_type": case_type,
                    "tenant": tenant,
                    "environment": environment,
                },
                queue_code=queue_code,
                script_gen_key=case_type,
                infra_type_ref=case_type,
                mode="kubectl",
                template_path=os.path.abspath(
                    os.path.join(_HELM_JOBS_BASE, case_type, "job.yaml")
                ),
            )]

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        if case_type in ('dynamodb', 'dynamo', 'dynamodb-table', 'dynamodb_table', 'table_management'):
            identifier = config_snapshot.get('identifier', '')
            identifier_sanitized = cls._sanitize_identifier(identifier) if identifier else 'default-table'

            infra_repo, infra_branch = await cls._resolve_infra_repo(tenant)

            if workflow_context.skip_commit:
                feature_branch = infra_branch
            else:
                feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=infra_repo,
                    base_branch=infra_branch,
                    tenant=tenant,
                    pr_type="infrastructure",
                )

            # Reconstruct the Terraform-prefixed table name so the ARN we
            # grant matches what the dynamodb layer actually creates.
            from app.core.config import settings as _settings
            account_id = config_snapshot.get('account_id') or _settings.onboarding_default_account_id
            region = config_snapshot.get('region') or _settings.onboarding_default_region
            terraform_table_name = (
                config_snapshot.get('terraform_table_name')
                or config_snapshot.get('table_name')
                or (
                    f"{tenant}-{_settings.onboarding_default_env}"
                    f"-{_settings.onboarding_default_region_code}"
                    f"-{_settings.onboarding_default_index}"
                    f"-{identifier_sanitized}"
                )
            )
            table_arn = f"arn:aws:dynamodb:{region}:{account_id}:table/{terraform_table_name}"

            files = [
                FileLocationItem(
                    repo=infra_repo,
                    file_path=f"environment/{tenant}/01/aws/us-east-1/dynamo/{identifier_sanitized}/terragrunt.hcl",
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "identifier": identifier,
                    },
                    queue_code=queue_code,
                    base_branch=infra_branch,
                    target_branch=infra_branch,
                    feature_branch=feature_branch,
                    script_gen_key="table_management",
                    infra_type_ref=case_type,
                ),
                cls._role_policy_item(
                    tenant=tenant, infra_repo=infra_repo, infra_branch=infra_branch,
                    feature_branch=feature_branch, queue_code=queue_code,
                    case_type=case_type, environment=environment,
                    policy_kind="dynamodb-rw", resource_arns=table_arn,
                ),
            ]

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        # ── Delete cases that revoke IAM access via PR ──────────────────────
        # Native AWS resources (S3, SQS, DynamoDB, Redis) added an IAM grant
        # to the tenant's default-role at create time. On delete, mirror that
        # with a "remove" mutation against the same role policy file. The
        # mutation rides the existing PR workflow — commit on a feature
        # branch, open PR, auto-merge for PaaS tenants, IAM Jenkins applies
        # the merge to AWS. Cascade post-action runs in parallel for DB
        # cleanup (variable_mst soft-delete, infra status → SOFT_DELETED).
        if case_type in (
            'delete_bucket',
            'delete_queue',
            'delete_dynamodb_table',
            'delete_redis',
            'delete_server',
        ):
            iam_revoke_item = await cls._build_delete_iam_revoke_item(
                tenant=tenant,
                case_type=case_type,
                environment=environment,
                queue_code=queue_code,
                config_snapshot=config_snapshot,
                workflow_context=workflow_context,
            )
            files = [iam_revoke_item] if iam_revoke_item else []
            response = FileLocationResponse(files=files)
            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}
            return response

        # ── Delete cases that have no IAM grant to revoke ───────────────────
        # K8s Postgres has no AWS IAM policy (in-cluster). EKS service uses
        # IRSA on its service account, not the shared default-role. Both
        # are handled entirely by their post-action components (DB cleanup
        # only — no PR, no IAM commit).
        if case_type in (
            'delete_k8s_postgres_create_server',
            'delete_service',
        ):
            response = FileLocationResponse(files=[])
            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}
            return response

        if case_type in ('s3', 's3-bucket', 's3_bucket', 'create_bucket'):
            identifier = config_snapshot.get('identifier', '')
            identifier_sanitized = cls._sanitize_identifier(identifier) if identifier else 'default-bucket'

            infra_repo, infra_branch = await cls._resolve_infra_repo(tenant)

            if workflow_context.skip_commit:
                feature_branch = infra_branch
            else:
                feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=infra_repo,
                    base_branch=infra_branch,
                    tenant=tenant,
                    pr_type="infrastructure",
                )

            from app.core.config import settings as _settings
            terraform_bucket_name = (
                config_snapshot.get('terraform_bucket_name')
                or config_snapshot.get('bucket_name')
                or (
                    f"{tenant}-{_settings.onboarding_default_env}"
                    f"-{_settings.onboarding_default_region_code}"
                    f"-{_settings.onboarding_default_index}"
                    f"-{identifier_sanitized}"
                )
            )
            bucket_arn = f"arn:aws:s3:::{terraform_bucket_name}"

            files = [
                FileLocationItem(
                    repo=infra_repo,
                    file_path=f"environment/{tenant}/01/aws/us-east-1/buckets/{identifier_sanitized}/terragrunt.hcl",
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "identifier": identifier,
                    },
                    queue_code=queue_code,
                    base_branch=infra_branch,
                    target_branch=infra_branch,
                    feature_branch=feature_branch,
                    script_gen_key="create_bucket",
                    infra_type_ref=case_type,
                ),
                # S3 needs bucket-level and object-level ARNs in one statement.
                cls._role_policy_item(
                    tenant=tenant, infra_repo=infra_repo, infra_branch=infra_branch,
                    feature_branch=feature_branch, queue_code=queue_code,
                    case_type=case_type, environment=environment,
                    policy_kind="s3-rw",
                    resource_arns=[bucket_arn, f"{bucket_arn}/*"],
                ),
            ]

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        if case_type in ('sqs', 'sqs-queue', 'sqs_queue', 'create_queue'):
            identifier = config_snapshot.get('identifier', '')
            identifier_sanitized = cls._sanitize_identifier(identifier) if identifier else 'default-queue'

            infra_repo, infra_branch = await cls._resolve_infra_repo(tenant)

            if workflow_context.skip_commit:
                feature_branch = infra_branch
            else:
                feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=infra_repo,
                    base_branch=infra_branch,
                    tenant=tenant,
                    pr_type="infrastructure",
                )

            from app.core.config import settings as _settings
            from app.domain.factories.infrastructure_mst_factory import (
                resolve_sqs_name, resolve_sqs_dlq_name, resolve_sqs_urls,
            )
            account_id = config_snapshot.get('account_id') or _settings.onboarding_default_account_id
            region = config_snapshot.get('region') or _settings.onboarding_default_region
            env = config_snapshot.get('env') or _settings.onboarding_default_env
            index = _settings.onboarding_default_index
            sqs_region_code = _settings.onboarding_default_region_code
            def _as_bool(v):
                return v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes")
            fifo_queue = _as_bool(config_snapshot.get('fifo_queue', False))
            create_dlq = _as_bool(config_snapshot.get('create_dlq', False))
            default_queue_name = resolve_sqs_name(
                identifier_sanitized, tenant, env, sqs_region_code, index, fifo_queue=fifo_queue,
            )
            # Prefer explicit values from the snapshot/locator (factory writes
            # `queue_name` = resolved AWS name); fall back to recomputing.
            terraform_queue_name = (
                config_snapshot.get('terraform_queue_name')
                or config_snapshot.get('queue_name')
                or default_queue_name
            )
            queue_arn = (
                config_snapshot.get('queue_arn')
                or resolve_sqs_urls(terraform_queue_name, region, account_id)[1]
            )
            # Grant IAM on DLQ too when it exists; otherwise the main-queue ARN alone.
            if create_dlq:
                dlq_name = (
                    config_snapshot.get('dlq_name')
                    or resolve_sqs_dlq_name(
                        identifier_sanitized, tenant, env, sqs_region_code, index, fifo_queue=fifo_queue,
                    )
                )
                dlq_arn = (
                    config_snapshot.get('dlq_arn')
                    or resolve_sqs_urls(dlq_name, region, account_id)[1]
                )
                sqs_iam_arns = [queue_arn, dlq_arn]
            else:
                sqs_iam_arns = queue_arn

            files = [
                FileLocationItem(
                    repo=infra_repo,
                    file_path=f"environment/{tenant}/01/aws/us-east-1/queues/{identifier_sanitized}/terragrunt.hcl",
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "identifier": identifier,
                    },
                    queue_code=queue_code,
                    base_branch=infra_branch,
                    target_branch=infra_branch,
                    feature_branch=feature_branch,
                    script_gen_key="create_queue",
                    infra_type_ref=case_type,
                ),
                cls._role_policy_item(
                    tenant=tenant, infra_repo=infra_repo, infra_branch=infra_branch,
                    feature_branch=feature_branch, queue_code=queue_code,
                    case_type=case_type, environment=environment,
                    policy_kind="sqs-rw", resource_arns=sqs_iam_arns,
                ),
            ]

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        if case_type == 'create_server' and infrastructuretype_ref_code in (
            'aurora_postgres_infrastructuretype_ref',
            'aurora_mysql_infrastructuretype_ref',
        ):
            identifier = config_snapshot.get('db_server_name') or config_snapshot.get('identifier', '')
            identifier_sanitized = cls._sanitize_identifier(identifier) if identifier else 'default-aurora'

            infra_repo, infra_branch = await cls._resolve_infra_repo(tenant)

            if workflow_context.skip_commit:
                feature_branch = infra_branch
            else:
                feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=infra_repo,
                    base_branch=infra_branch,
                    tenant=tenant,
                    pr_type="infrastructure",
                )

            files = [FileLocationItem(
                repo=infra_repo,
                file_path=f"environment/{tenant}/01/aws/us-east-1/rds/{identifier_sanitized}/terragrunt.hcl",
                config={
                    "case_type": case_type,
                    "tenant": tenant,
                    "environment": environment,
                    "identifier": identifier,
                },
                queue_code=queue_code,
                base_branch=infra_branch,
                target_branch=infra_branch,
                feature_branch=feature_branch,
                script_gen_key="create_server",
                infra_type_ref=infrastructuretype_ref_code,
            )]

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        if case_type == 'database_creation' and infrastructuretype_ref_code in (
            'aurora_postgres_infrastructuretype_ref',
            'aurora_mysql_infrastructuretype_ref',
        ):
            identifier = config_snapshot.get('db_server_name') or config_snapshot.get('identifier', '')
            identifier_sanitized = cls._sanitize_identifier(identifier) if identifier else 'default-aurora'

            infra_repo, infra_branch = await cls._resolve_infra_repo(tenant)

            if workflow_context.skip_commit:
                feature_branch = infra_branch
            else:
                feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=infra_repo,
                    base_branch=infra_branch,
                    tenant=tenant,
                    pr_type="infrastructure",
                )

            files = [FileLocationItem(
                repo=infra_repo,
                file_path=f"environment/{tenant}/01/aws/us-east-1/rds/{identifier_sanitized}/terragrunt.hcl",
                config={
                    "case_type": case_type,
                    "tenant": tenant,
                    "environment": environment,
                    "identifier": identifier,
                },
                queue_code=queue_code,
                base_branch=infra_branch,
                target_branch=infra_branch,
                feature_branch=feature_branch,
                script_gen_key="database_creation",
                infra_type_ref=infrastructuretype_ref_code,
            )]

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        if case_type in ('redis', 'elasticache', 'elasticache_redis', 'create_redis'):
            identifier = config_snapshot.get('redis_cluster_name') or config_snapshot.get('identifier', '')
            identifier_sanitized = cls._sanitize_identifier(identifier) if identifier else 'default-redis'

            infra_repo, infra_branch = await cls._resolve_infra_repo(tenant)

            if workflow_context.skip_commit:
                feature_branch = infra_branch
            else:
                feature_branch = await cls._get_or_create_feature_branch(
                    workflow_context=workflow_context,
                    repo=infra_repo,
                    base_branch=infra_branch,
                    tenant=tenant,
                    pr_type="infrastructure",
                )

            from app.core.config import settings as _settings
            replication_group_name = (
                f"{tenant}-{_settings.onboarding_default_env}"
                f"-{_settings.onboarding_default_region_code}"
                f"-{_settings.onboarding_default_index}"
                f"-{identifier_sanitized}"
            )
            replication_group_arn = (
                f"arn:aws:elasticache:{_settings.onboarding_default_region}"
                f":{_settings.onboarding_default_account_id}"
                f":replicationgroup:{replication_group_name}"
            )
            user_arn = (
                f"arn:aws:elasticache:{_settings.onboarding_default_region}"
                f":{_settings.onboarding_default_account_id}"
                f":user:{identifier_sanitized}-iam"
            )

            files = [
                FileLocationItem(
                    repo=infra_repo,
                    file_path=f"environment/{tenant}/01/aws/us-east-1/redis/{identifier_sanitized}/terragrunt.hcl",
                    config={
                        "case_type": case_type,
                        "tenant": tenant,
                        "environment": environment,
                        "identifier": identifier,
                    },
                    queue_code=queue_code,
                    base_branch=infra_branch,
                    target_branch=infra_branch,
                    feature_branch=feature_branch,
                    script_gen_key="create_redis",
                    infra_type_ref=case_type,
                ),
                cls._role_policy_item(
                    tenant=tenant, infra_repo=infra_repo, infra_branch=infra_branch,
                    feature_branch=feature_branch, queue_code=queue_code,
                    case_type=case_type, environment=environment,
                    policy_kind="redis-connect",
                    resource_arns=[replication_group_arn, user_arn],
                ),
            ]

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        # ── EKS service operations: stop, restart, delete ──
        if case_type in ('stop_service', 'restart_service', 'delete_service'):
            service_name = config_snapshot.get('service_name', '')
            if service_name.lower().endswith('-service'):
                service_name = service_name[:-8]

            service_name_sanitized = cls._sanitize_identifier(service_name) if service_name else ''

            if not service_name_sanitized:
                raise ValueError(
                    "config_snapshot.service_name is required for EKS service operations"
                )

            # These are inline Jenkins operations — no git commit needed
            files = [FileLocationItem(
                repo="",
                file_path="",
                config={
                    "case_type": case_type,
                    "tenant": tenant,
                    "environment": environment,
                },
                queue_code=queue_code,
                script_gen_key="eks_service_ops",
                infra_type_ref=infrastructuretype_ref_code or case_type,
                mode="jenkins",
            )]

            response = FileLocationResponse(files=files)

            queue_id = queue_item.get('id')
            if queue_id and workflow_context:
                workflow_context.file_location_responses[queue_id] = response
                if queue_id not in workflow_context.script_gen_responses:
                    workflow_context.script_gen_responses[queue_id] = {}

            return response

        raise NotImplementedError(
            f"DefaultFileLocator: no handler for case_type='{case_type}', "
            f"infrastructuretype_ref_code='{infrastructuretype_ref_code}'"
        )

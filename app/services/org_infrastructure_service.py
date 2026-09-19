"""
Organization Infrastructure Provisioning Service

When a new organization signs up, this service:
1. Creates a per-org GitHub repo (Devlift-ai/{subdomain}-infrastructure)
2. Shallow-clones the source infra repo to a temp directory
3. Removes excluded paths (environment/, READMEs)
4. Adds rendered tenant-specific environment terragrunt files
5. Pushes everything to the new repo in a single git push

No PRs — the repo is ready to run CI/CD immediately after provisioning.

The directory structure under environment/ uses the subdomain as the top-level folder,
so every tenant gets fully isolated VPC + EKS naming:
  VPC name:     {subdomain}-trial-mumbai-01-main-vpc
  EKS cluster:  {subdomain}-trial-mumbai-01-main-cluster
"""

import os
import shutil
import asyncio
import logging
import tempfile
import aiofiles
from typing import Dict, Any, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update, select
from app.core.config import settings
from app.core.enum import EnvironmentEnum, DeploymentStatusEnum, InfraVendorEnum, LogProviderEnum
from app.db.models.tenants_mst_model import TenantsMstModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.infra_vendor_accounts_mst_model import InfraVendorAccountsMstModel
from app.repository.infrastructure_mst_repository import InfrastructureMstRepository
from app.repository.infra_vendor_accounts_mst_repository import InfraVendorAccountsMstRepository
from app.repository.log_provider_config_repository import LogProviderConfigRepository
from app.repository.namespace_mst_repository import NamespaceMstRepository
from app.utils.eks_bootstrap import (
    build_ingress_class_yaml,
    build_namespace_yaml,
    build_seed_ingress_yaml,
)
from app.domain.factories.infrastructure_mst_factory import make_infrastructure_mst_eks
from app.integrations.github_integration import GitHubIntegration
from app.utils.github_app_token import get_token
from app.services.vpc_and_resource_discovery_service import _MOCK_CANVAS_DATA
from app.services.github_app_installation_service import GitHubAppInstallationService
from app.services.infrastructure_mst_service import InfrastructureMstService

logger = logging.getLogger(__name__)

# Template directory (env.hcl, root.hcl, base/terragrunt.hcl, eks/terragrunt.hcl)
TEMPLATE_DIR = os.path.join(
    os.path.dirname(__file__),
    "../../templates/terragrunt/org-onboarding"
)

# Source infrastructure repo to clone scaffold from (loaded from settings / .env)
SOURCE_INFRA_ORG = settings.onboarding_source_org
SOURCE_INFRA_REPO = settings.onboarding_source_repo
SOURCE_INFRA_BRANCH = settings.onboarding_source_branch

# Paths to remove after cloning (not needed in per-org repos)
SCAFFOLD_EXCLUDE_PATHS = [
    "environment",
    "README.md",
    "claude-recommendations.md",
    "gpt-recommendation.md",
    "recommendations.md",
]

# Default infrastructure settings (loaded from settings / .env)
DEFAULT_REGION = settings.onboarding_default_region
DEFAULT_REGION_CODE = settings.onboarding_default_region_code
DEFAULT_COUNTRY_CODE = settings.onboarding_default_country_code
DEFAULT_INDEX = settings.onboarding_default_index
DEFAULT_ACCOUNT_ID = settings.onboarding_default_account_id
DEFAULT_ENV = settings.onboarding_default_env
DEFAULT_CLUSTER_NAME = settings.onboarding_default_vm_name  # reused for EKS cluster folder name

# Bootstrap Jenkinsfile template
BOOTSTRAP_JENKINSFILE_TEMPLATE = os.path.join(
    os.path.dirname(__file__),
    "../../templates/eks/bootstrap/Jenkinsfile-bootstrap"
)


async def _load_template(relative_path: str) -> str:
    """Load a template file from the org-onboarding template directory."""
    template_path = os.path.join(TEMPLATE_DIR, relative_path)
    async with aiofiles.open(template_path, "r") as f:
        return await f.read()


def compute_default_role_name(subdomain: str) -> str:
    """Tenant default role name as produced by the application Terraform layer
    when invoked with identifier=\"default\" and the onboarding context."""
    return (
        f"{subdomain}-{DEFAULT_ENV}-{DEFAULT_REGION_CODE}-{DEFAULT_INDEX}"
        f"-default-role"
    )


def _render_env_hcl(
    template: str,
    organization: str,
    env: str = DEFAULT_ENV,
    account_id: str = DEFAULT_ACCOUNT_ID,
    region: str = DEFAULT_REGION,
    region_code: str = DEFAULT_REGION_CODE,
    country_code: str = DEFAULT_COUNTRY_CODE,
    index: str = DEFAULT_INDEX,
) -> str:
    """Render the env.hcl template with organization-specific values."""
    rendered = template
    rendered = rendered.replace("${organization}", organization)
    rendered = rendered.replace("${env}", env)
    rendered = rendered.replace("${account_id}", account_id)
    rendered = rendered.replace("${region}", region)
    rendered = rendered.replace("${region_code}", region_code)
    rendered = rendered.replace("${country_code}", country_code)
    rendered = rendered.replace("${index}", index)
    return rendered


async def _run_git(cmd: List[str], cwd: str) -> str:
    """Run a git command asynchronously and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git command failed: {' '.join(cmd)}\n"
            f"stderr: {stderr.decode()}"
        )
    return stdout.decode().strip()


class OrgInfrastructureService:
    """
    Provisions infrastructure (VPC + EKS) for a new organization.

    Uses git clone + push instead of per-file API calls for speed.
    Clones the source infra repo, removes excluded paths, adds tenant
    env files, and pushes to the new per-org repo.

    Repo structure created:
      .gitignore
      .terraform-version
      .terragrunt-version
      versions.hcl
      .github/workflows/           (terragrunt-apply, manual, run-all)
      layers/aws/v1/base/          (VPC + deploy role Terraform module)
      layers/aws/v1/eks/           (EKS cluster Terraform module)
      environment/{subdomain}/01/aws/{region}/
        ├── env.hcl
        ├── root.hcl
        ├── base/main/terragrunt.hcl
        └── eks/main/terragrunt.hcl
    """

    EKS_INFRA_TYPE_REF = "eks_infrastructuretype_ref"

    def __init__(self, db: AsyncSession):
        self.logger = logging.getLogger(__name__)
        self.db = db
        self.infra_repo = InfrastructureMstRepository(db)
        self.vendor_repo = InfraVendorAccountsMstRepository(db)
        self.log_provider_repo = LogProviderConfigRepository(db)

    async def _get_github_token(self) -> str:
        """Get GitHub App token using the platform installation ID from .env."""
        return await get_token(settings.github_app_platform_installation_id)

    async def _build_environment_files(
        self,
        subdomain: str,
        github_org: str = "",
        region: str = DEFAULT_REGION,
    ) -> List[Dict[str, str]]:
        """Build the list of tenant-specific environment terragrunt files."""
        base_path = f"environment/{subdomain}/{DEFAULT_INDEX}/aws/{region}"

        env_template = await _load_template("env.hcl")
        root_content = await _load_template("root.hcl")
        base_content = await _load_template("base/terragrunt.hcl")
        eks_content = await _load_template("eks/terragrunt.hcl")
        default_role_content = await _load_template("default-role/terragrunt.hcl")

        env_content = _render_env_hcl(env_template, organization=subdomain)

        # Render allowed_github_orgs into base template
        orgs = [settings.onboarding_source_org]
        if github_org and github_org != settings.onboarding_source_org:
            orgs.append(github_org)
        orgs_hcl = "[{}]".format(", ".join(f'"{org}"' for org in orgs))
        base_content = base_content.replace("${allowed_github_orgs}", orgs_hcl)

        # Render the default-role terragrunt with tenant namespace + cluster name.
        tenant_namespace = f"{subdomain}-ns"
        eks_cluster_name = (
            f"devlift-{DEFAULT_ENV}-{DEFAULT_REGION_CODE}-{DEFAULT_INDEX}-"
            f"{DEFAULT_CLUSTER_NAME}-cluster"
        )
        default_role_content = default_role_content.replace(
            "${tenant_namespace}", tenant_namespace
        ).replace(
            "${eks_cluster_name}", eks_cluster_name
        )

        return [
            {"path": f"{base_path}/env.hcl", "content": env_content},
            {"path": f"{base_path}/root.hcl", "content": root_content},
            {"path": f"{base_path}/base/main/terragrunt.hcl", "content": base_content},
            {"path": f"{base_path}/eks/{DEFAULT_CLUSTER_NAME}/terragrunt.hcl", "content": eks_content},
            {"path": f"{base_path}/application/default/terragrunt.hcl", "content": default_role_content},
        ]

    async def _save_tenant_config(self, subdomain: str, infra_repository: str, infra_branch: str) -> None:
        """Save the infrastructure repo details into the tenant's config JSONB column."""
        stmt = (
            update(TenantsMstModel)
            .where(TenantsMstModel.code == subdomain, TenantsMstModel.is_deleted == False)
            .values(config={"github": {"infra_repository": infra_repository, "infra_branch": infra_branch}})
        )
        await self.db.execute(stmt)
        await self.db.commit()
        self.logger.info(f"[INFRA] Saved tenant config: {infra_repository} @ {infra_branch} for '{subdomain}'")

    async def _ensure_vendor_account(self, subdomain: str) -> str:
        """Ensure an infra_vendor_accounts_mst record exists for this tenant. Returns the code."""
        vendor_account_code = f"aws_{subdomain}_stage"

        existing = await self.vendor_repo.get_by_tenant_and_vendor(
            tenant_code=subdomain,
            infra_vendor_enum=InfraVendorEnum.aws,
            environments_enum=EnvironmentEnum.stage,
        )
        if existing:
            self.logger.info(f"[INFRA] Vendor account already exists: {existing.code}")
            return existing.code

        # Role name from base layer: ${context.organization}-deploy-role
        deploy_role_name = f"{subdomain}-deploy-role"
        default_role_name = compute_default_role_name(subdomain)

        vendor_account = await self.vendor_repo.create(
            code=vendor_account_code,
            name=f"AWS {subdomain.capitalize()} Stage Account",
            description=f"AWS account config for {subdomain} - Stage environment",
            tenants_mst_code=subdomain,
            applications_mst_code=None,
            resource_group_mst_code=None,
            auth_config={
                "authentication_type": "iam_role",
                "account_id": DEFAULT_ACCOUNT_ID,
                "region": DEFAULT_REGION,
                "assume_role_arn": f"arn:aws:iam::{DEFAULT_ACCOUNT_ID}:role/Devlift-PlatformAccess",
                "github_role_arn": f"arn:aws:iam::{DEFAULT_ACCOUNT_ID}:role/Devlift-github-role",
                "deploy_role_arn": f"arn:aws:iam::{DEFAULT_ACCOUNT_ID}:role/{deploy_role_name}",
                "default_role_arn": f"arn:aws:iam::{DEFAULT_ACCOUNT_ID}:role/{default_role_name}",
                "external_id": settings.secrets_external_id,
                "session_name": "DevliftSession",
                "session_duration": 3600,
            },
            environments_enum=EnvironmentEnum.stage,
            infra_vendor_enum=InfraVendorEnum.aws,
        )
        self.logger.info(f"[INFRA] Created vendor account: {vendor_account.code}")

        # Auto-create CloudWatch log provider config with same assume_role_arn
        await self._ensure_log_provider_config(subdomain, vendor_account.auth_config)

        return vendor_account.code

    async def _ensure_log_provider_config(self, tenant_code: str, auth_config: dict) -> None:
        """Create a default CloudWatch log provider config for the tenant using vendor account credentials."""
        existing = await self.log_provider_repo.get_by_tenant_and_provider(
            tenant_code, LogProviderEnum.CLOUDWATCH
        )
        if existing:
            return

        await self.log_provider_repo.create(
            code=f"lpc-cw-{tenant_code}",
            name="CloudWatch Logs",
            description=f"CloudWatch log provider for {tenant_code}",
            tenants_mst_code=tenant_code,
            provider=LogProviderEnum.CLOUDWATCH,
            auth_config={
                "authentication_type": auth_config.get("authentication_type", "iam_role"),
                "assume_role_arn": auth_config.get("assume_role_arn", ""),
                "region": auth_config.get("region", settings.cloudwatch_logs_region),
                "external_id": auth_config.get("external_id", ""),
                "session_name": auth_config.get("session_name", "DevliftLogsSession"),
                "session_duration": auth_config.get("session_duration", 3600),
            },
            is_default=True,
        )
        self.logger.info(f"[INFRA] Created CloudWatch log provider config for tenant: {tenant_code}")

    async def _ensure_devlift_k8s_vendor_account(self, subdomain: str) -> str:
        """Ensure a devlift_k8s vendor account exists for this tenant. Returns the code."""
        vendor_account_code = f"devlift_k8s_{subdomain}_stage"

        existing = await self.vendor_repo.get_by_tenant_and_vendor(
            tenant_code=subdomain,
            infra_vendor_enum=InfraVendorEnum.devlift_k8s,
            environments_enum=EnvironmentEnum.stage,
        )
        if existing:
            self.logger.info(f"[INFRA] DevLift K8s vendor account already exists: {existing.code}")
            return existing.code

        deploy_role_name = f"{subdomain}-deploy-role"
        default_role_name = compute_default_role_name(subdomain)

        vendor_account = await self.vendor_repo.create(
            code=vendor_account_code,
            name=f"DevLift K8s {subdomain.capitalize()} Stage Account",
            description=f"DevLift K8s account config for {subdomain} - Stage environment",
            tenants_mst_code=subdomain,
            applications_mst_code=None,
            resource_group_mst_code=None,
            auth_config={
                "authentication_type": "iam_role",
                "account_id": DEFAULT_ACCOUNT_ID,
                "region": DEFAULT_REGION,
                "assume_role_arn": f"arn:aws:iam::{DEFAULT_ACCOUNT_ID}:role/Devlift-PlatformAccess",
                "github_role_arn": f"arn:aws:iam::{DEFAULT_ACCOUNT_ID}:role/Devlift-github-role",
                "deploy_role_arn": f"arn:aws:iam::{DEFAULT_ACCOUNT_ID}:role/{deploy_role_name}",
                "default_role_arn": f"arn:aws:iam::{DEFAULT_ACCOUNT_ID}:role/{default_role_name}",
                "external_id": settings.secrets_external_id,
                "session_name": "DevliftSession",
                "session_duration": 3600,
            },
            environments_enum=EnvironmentEnum.stage,
            infra_vendor_enum=InfraVendorEnum.devlift_k8s,
        )
        self.logger.info(f"[INFRA] Created DevLift K8s vendor account: {vendor_account.code}")
        return vendor_account.code

    async def _create_eks_infrastructure_record(
        self,
        subdomain: str,
        infra_status: DeploymentStatusEnum = DeploymentStatusEnum.GIT_COMMITTED,
    ) -> None:
        """Create an infrastructure_mst entry for the EKS cluster being provisioned."""
        # Look up the trail application for this tenant
        stmt = (
            select(ApplicationsMstModel)
            .where(
                ApplicationsMstModel.tenants_mst_code == subdomain,
                ApplicationsMstModel.name == f"{subdomain}_trail",
                ApplicationsMstModel.is_deleted == False,
            )
        )
        result = await self.db.execute(stmt)
        trail_app = result.scalar_one_or_none()

        if not trail_app:
            self.logger.warning(f"[INFRA] Trail application not found for tenant '{subdomain}', skipping infra record")
            return

        cluster_name = f"devlift-{DEFAULT_ENV}-{DEFAULT_REGION_CODE}-{DEFAULT_INDEX}-{DEFAULT_CLUSTER_NAME}-cluster"

        # Check if EKS record already exists for this tenant + cluster
        existing = await self.infra_repo.list_by_filters(
            tenant_code=subdomain,
            cluster_name=cluster_name,
        )
        if existing:
            self.logger.info(f"[INFRA] EKS infrastructure record already exists: {existing[0].code}")
            return

        vendor_account_code = await self._ensure_vendor_account(subdomain)
        await self._ensure_devlift_k8s_vendor_account(subdomain)

        cluster_arn = f"arn:aws:eks:{DEFAULT_REGION}:{DEFAULT_ACCOUNT_ID}:cluster/{cluster_name}"
        locator = {
            "cluster_name": cluster_name,
            "cluster_arn": cluster_arn,
            "region": DEFAULT_REGION,
            "cloudRegion": DEFAULT_REGION,
            "cloudRegionId": f"cr-000000000000-{DEFAULT_REGION}",
            "vpcId": "vpc-placeholder-main",
            "subnetIds": [
                "subnet-placeholder-private-1a",
                "subnet-placeholder-private-1b",
            ],
            "acm_cert_arn": settings.onboarding_default_acm_cert_arn or settings.acm_wildcard_cert_arn,
            "efs_volume_handle": settings.onboarding_default_efs_volume_handle,
        }

        eks_data = make_infrastructure_mst_eks(
            identifier=cluster_name,
            tenant_code=subdomain,
            environment=EnvironmentEnum.stage,
            region=DEFAULT_REGION,
            infrastructuretype_ref_code=self.EKS_INFRA_TYPE_REF,
            infra_vendor_accounts_mst_code=vendor_account_code,
            locator=locator,
            infra_status=infra_status,
            infra_status_updated_by="system",
            geo_loc_mst_code=f"region-{subdomain}-us",
        )

        created = await self.infra_repo.create(**eks_data)
        self.logger.info(
            f"[INFRA] Created EKS infrastructure record: {created.code} "
            f"(status: {infra_status.value})"
        )

        # Bootstrap the shared namespace + seed ingress via Jenkins pipeline.
        await self._bootstrap_tenant_namespace(
            subdomain=subdomain,
            infra_code=created.code,
            cert_arn=locator["acm_cert_arn"],
        )

    async def _bootstrap_tenant_namespace(
        self,
        subdomain: str,
        infra_code: str,
        cert_arn: str,
        env: str = "stage",
    ) -> None:
        """
        Create the shared namespace DB record, commit bootstrap K8s manifests to the
        infra GitHub repo, and trigger the Jenkins bootstrap pipeline.

        The Jenkins pipeline:
        1. Waits for the ACM cert to be validated
        2. Applies namespace.yaml + seed-ingress.yaml → shared ALB is provisioned
        3. Posts final webhook with the ALB hostname

        Non-blocking — a failure here logs a warning but does not abort signup.
        """
        namespace = f"{subdomain}-ns"
        ns_repo = NamespaceMstRepository(self.db)

        # Idempotency: skip if the namespace record already exists.
        existing = await ns_repo.check_namespace_exists(infra_code, namespace)
        if existing:
            self.logger.info(
                f"[INFRA] Shared namespace record already exists for '{namespace}', skipping bootstrap"
            )
            return

        # Persist the namespace record.
        ns_record = await ns_repo.create_namespace(
            code=f"ns-{subdomain}-{env}",
            name=namespace,
            infrastructure_mst_code=infra_code,
            namespace=namespace,
            description="Auto-created shared namespace",
        )
        self.logger.info(f"[INFRA] Created namespace_mst record: {ns_record.code} ({namespace})")

        # Commit bootstrap manifests to the infra repo + trigger Jenkins pipeline.
        try:
            await self._commit_and_trigger_bootstrap(
                subdomain=subdomain,
                env=env,
                infra_code=infra_code,
                cert_arn=cert_arn,
            )
        except Exception as exc:
            self.logger.warning(
                f"[INFRA] Bootstrap pipeline setup failed for '{namespace}' (non-blocking): {exc}"
            )

    async def _commit_and_trigger_bootstrap(
        self,
        subdomain: str,
        env: str,
        infra_code: str,
        cert_arn: str,
    ) -> None:
        """
        Commit namespace.yaml + seed-ingress.yaml to the infra repo and trigger
        the Jenkins bootstrap pipeline for this tenant.
        """
        from app.services.jenkins_provisioning_service import JenkinsProvisioningService

        token = await self._get_github_token()
        base_url = settings.github_base_url.rstrip("/")
        org = settings.onboarding_source_org
        repo_name = f"{subdomain}-infrastructure"
        namespace = f"{subdomain}-ns"
        manifests_path = f"eks-bootstrap/{namespace}"

        # Build manifest YAML strings.
        namespace_yaml = build_namespace_yaml(namespace)
        ingress_class_yaml = await build_ingress_class_yaml(
            tenant_code=subdomain,
            env=env,
            acm_cert_arn=cert_arn,
        )
        seed_ingress_yaml = await build_seed_ingress_yaml(
            namespace=namespace,
            tenant_code=subdomain,
            env=env,
            acm_cert_arn=cert_arn,
        )

        # Commit all three files to the infra repo via GitHub API.
        await GitHubIntegration.update_or_create_file(
            token=token,
            base_url=base_url,
            owner=org,
            repo=repo_name,
            branch=SOURCE_INFRA_BRANCH,
            file_path=f"{manifests_path}/namespace.yaml",
            content=namespace_yaml,
            message=f"bootstrap: add shared namespace manifest for {namespace}",
        )
        await GitHubIntegration.update_or_create_file(
            token=token,
            base_url=base_url,
            owner=org,
            repo=repo_name,
            branch=SOURCE_INFRA_BRANCH,
            file_path=f"{manifests_path}/ingressclass.yaml",
            content=ingress_class_yaml,
            message=f"bootstrap: add shared ALB IngressClass for {namespace}",
        )
        await GitHubIntegration.update_or_create_file(
            token=token,
            base_url=base_url,
            owner=org,
            repo=repo_name,
            branch=SOURCE_INFRA_BRANCH,
            file_path=f"{manifests_path}/seed-ingress.yaml",
            content=seed_ingress_yaml,
            message=f"bootstrap: add seed ingress manifest for {namespace}",
        )
        self.logger.info(
            f"[INFRA] Bootstrap manifests committed to {org}/{repo_name}/{manifests_path}"
        )

        # Render the bootstrap Jenkinsfile.
        with open(BOOTSTRAP_JENKINSFILE_TEMPLATE, "r") as f:
            jenkinsfile = f.read()

        cluster_name = f"devlift-{DEFAULT_ENV}-{DEFAULT_REGION_CODE}-{DEFAULT_INDEX}-{DEFAULT_CLUSTER_NAME}-cluster"
        webhook_url = f"{settings.devlift_backend_url.rstrip('/')}/api/v1/jenkins/webhook"

        jenkinsfile = jenkinsfile.replace("{{AWS_REGION}}", DEFAULT_REGION)
        jenkinsfile = jenkinsfile.replace("{{EKS_CLUSTER_NAME}}", cluster_name)
        jenkinsfile = jenkinsfile.replace("{{NAMESPACE}}", namespace)
        jenkinsfile = jenkinsfile.replace("{{INFRA_REPO}}", f"{org}/{repo_name}")
        jenkinsfile = jenkinsfile.replace("{{INFRA_BRANCH}}", SOURCE_INFRA_BRANCH)
        jenkinsfile = jenkinsfile.replace("{{MANIFESTS_PATH}}", manifests_path)
        jenkinsfile = jenkinsfile.replace("{{OBSTOOL_WEBHOOK_URL}}", webhook_url)
        jenkinsfile = jenkinsfile.replace("{{WEBHOOK_SECRET}}", settings.pipeline_webhook_secret or "")

        # Provision + trigger the Jenkins bootstrap pipeline.
        jenkins_service = JenkinsProvisioningService(self.db)
        result = await jenkins_service.provision_pipeline_job(
            job_name="eks-bootstrap",
            jenkinsfile_content=jenkinsfile,
            tenant_code=subdomain,
            environment=env,
            transaction_code=infra_code,
            table_name="INFRASTRUCTURE",
            trigger_build=True,
            build_params={"GITHUB_TOKEN": token},
        )
        self.logger.info(
            f"[INFRA] Bootstrap pipeline triggered: job={result.get('job_name')}, "
            f"run_code={result.get('run_code')}"
        )

    async def _build_paas_environment_files(
        self,
        subdomain: str,
        region: str = DEFAULT_REGION,
    ) -> List[Dict[str, str]]:
        """Build PaaS tenant environment files (env.hcl + root.hcl only, no base/eks terragrunt)."""
        base_path = f"environment/{subdomain}/{DEFAULT_INDEX}/aws/{region}"

        env_template = await _load_template("env.hcl")
        root_content = await _load_template("root.hcl")

        env_content = _render_env_hcl(env_template, organization=subdomain)

        return [
            {"path": f"{base_path}/env.hcl", "content": env_content},
            {"path": f"{base_path}/root.hcl", "content": root_content},
        ]

    async def provision(self, subdomain: str, github_org: str = "", paas: bool = False) -> Dict[str, Any]:
        """
        Provision infrastructure for a new organization.

        1. Creates a per-org GitHub repo via API
        2. Shallow-clones the source infra repo
        3. Removes excluded paths, adds tenant env files
        4. Changes remote to new repo and pushes

        Args:
            subdomain: Organization subdomain (used as org identifier / tenant ID)
            github_org: Tenant's GitHub org (from linked installation) for deploy role OIDC trust
            paas: If True, PaaS shared cluster mode — skip base/eks terragrunt,
                  create infra record with ACTIVE status.

        Returns:
            Dict with repo URL, status, and committed files
        """
        org = settings.onboarding_source_org
        repo_name = f"{subdomain}-infrastructure"

        self.logger.info(f"[INFRA] Provisioning infrastructure for tenant: {subdomain}")
        self.logger.info(f"[INFRA] Target repo: {org}/{repo_name}")

        token = await self._get_github_token()
        base_url = settings.github_base_url.rstrip("/")

        # Step 1: Create per-org repository (idempotent)
        repo_result = await GitHubIntegration.create_org_repository(
            token=token,
            base_url=base_url,
            org=org,
            repo_name=repo_name,
            description=f"Infrastructure repo for tenant {subdomain}",
            private=True,
            auto_init=False,  # Don't init — we'll push from clone
        )

        already_exists = repo_result.get("already_exists", False)
        if already_exists:
            self.logger.info(
                f"[INFRA] Repo already exists: {org}/{repo_name} — skipping git steps, seeding DB records"
            )
            infra_repo_full = f"{org}/{repo_name}"
            await self._save_tenant_config(subdomain, infra_repo_full, SOURCE_INFRA_BRANCH)
            try:
                infra_status = DeploymentStatusEnum.ACTIVE if paas else DeploymentStatusEnum.GIT_COMMITTED
                await self._create_eks_infrastructure_record(subdomain, infra_status=infra_status)
            except Exception as e:
                self.logger.warning(f"[INFRA] Failed to create EKS infra record (non-blocking): {e}")
                await self.db.rollback()
            return {
                "status": "success",
                "message": f"Infrastructure repo already exists, DB records seeded for tenant '{subdomain}'",
                "repo_url": repo_result.get("html_url"),
            }

        self.logger.info(f"[INFRA] Repo created: {org}/{repo_name}")

        # Step 2: Build authenticated git clone/push URLs (token already fetched above)
        source_clone_url = f"https://x-access-token:{token}@github.com/{SOURCE_INFRA_ORG}/{SOURCE_INFRA_REPO}.git"
        target_push_url = f"https://x-access-token:{token}@github.com/{org}/{repo_name}.git"

        tmp_dir = tempfile.mkdtemp(prefix=f"infra-{subdomain}-")

        # PaaS tenant repos hold only tenant-specific config — layers come from the
        # base infra repo at apply time. Enterprise tenants still inherit the full
        # scaffold so they have a self-contained repo.
        clone_dir: Optional[str] = None
        if not paas:
            clone_dir = tempfile.mkdtemp(prefix=f"infra-clone-{subdomain}-")

        try:
            if not paas:
                # Step 3: Clone the source repo into a separate temp dir
                self.logger.info(f"[INFRA] Cloning {SOURCE_INFRA_ORG}/{SOURCE_INFRA_REPO}...")
                await _run_git(
                    ["git", "clone", "--depth", "1", "--branch", SOURCE_INFRA_BRANCH, source_clone_url, "."],
                    cwd=clone_dir,
                )

                # Step 4: Copy files to working dir (excluding .git and excluded paths)
                exclude_set = set(SCAFFOLD_EXCLUDE_PATHS) | {".git"}
                for item in os.listdir(clone_dir):
                    if item in exclude_set:
                        continue
                    src = os.path.join(clone_dir, item)
                    dst = os.path.join(tmp_dir, item)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)

            # Step 5: Write tenant-specific environment files
            if paas:
                env_files = await self._build_paas_environment_files(subdomain)
            else:
                env_files = await self._build_environment_files(subdomain, github_org=github_org)
            for f in env_files:
                file_path = os.path.join(tmp_dir, f["path"])
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "w") as fh:
                    fh.write(f["content"])

            # Step 6: Init fresh repo, commit, and push (no inherited history)
            bot_name = f"{settings.github_app_slug}[bot]"
            bot_email = f"{settings.github_app_id}+{settings.github_app_slug}[bot]@users.noreply.github.com"
            await _run_git(["git", "init", "-b", SOURCE_INFRA_BRANCH], cwd=tmp_dir)
            await _run_git(["git", "config", "user.name", bot_name], cwd=tmp_dir)
            await _run_git(["git", "config", "user.email", bot_email], cwd=tmp_dir)
            await _run_git(["git", "add", "."], cwd=tmp_dir)
            commit_msg = (
                f"infra: initialize PaaS repo for tenant '{subdomain}'"
                if paas
                else f"infra: initialize repo for tenant '{subdomain}' — scaffold + VPC + EKS"
            )
            await _run_git(
                ["git", "commit", "-m", commit_msg],
                cwd=tmp_dir,
            )
            await _run_git(["git", "remote", "add", "origin", target_push_url], cwd=tmp_dir)
            await _run_git(["git", "push", "-u", "origin", SOURCE_INFRA_BRANCH], cwd=tmp_dir)

            self.logger.info(
                f"[INFRA] Infrastructure repo provisioned: {repo_result.get('html_url')} "
                f"({len(env_files)} env files{'' if paas else ' + scaffold'})"
            )

            # Step 7: Save the new repo into the tenant's config JSONB
            infra_repo_full = f"{org}/{repo_name}"
            await self._save_tenant_config(subdomain, infra_repo_full, SOURCE_INFRA_BRANCH)

            # Step 8: Set GitHub Actions secrets on the new repo (for pipeline webhook)
            try:
                if settings.pipeline_webhook_secret and settings.devlift_backend_url:
                    await GitHubIntegration.set_repo_secret(
                        token=token, base_url=base_url, owner=org, repo=repo_name,
                        secret_name="PIPELINE_WEBHOOK_SECRET",
                        secret_value=settings.pipeline_webhook_secret,
                    )
                    await GitHubIntegration.set_repo_secret(
                        token=token, base_url=base_url, owner=org, repo=repo_name,
                        secret_name="DEVLIFT_BACKEND_URL",
                        secret_value=settings.devlift_backend_url.rstrip("/"),
                    )
                    self.logger.info(f"[INFRA] Pipeline webhook secrets set on {org}/{repo_name}")
            except Exception as e:
                self.logger.warning(f"[INFRA] Failed to set repo secrets (non-blocking): {e}")

            # Step 9: Create infrastructure_mst record for EKS
            # PaaS: ACTIVE (shared cluster already running), Enterprise: GIT_COMMITTED (cluster being created)
            try:
                infra_status = DeploymentStatusEnum.ACTIVE if paas else DeploymentStatusEnum.GIT_COMMITTED
                await self._create_eks_infrastructure_record(subdomain, infra_status=infra_status)
            except Exception as e:
                self.logger.warning(f"[INFRA] Failed to create EKS infra record (non-blocking): {e}")
                await self.db.rollback()

            committed = [f["path"] for f in env_files]
            if not paas:
                committed.append("(scaffold from source repo)")
            return {
                "status": "success",
                "repo_url": repo_result.get("html_url"),
                "files_committed": committed,
            }

        except Exception as e:
            self.logger.error(f"[INFRA] Git-based provisioning failed: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}

        finally:
            # Clean up temp directories
            if clone_dir:
                shutil.rmtree(clone_dir, ignore_errors=True)
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def ensure_infra_repo_exists(self, owner: str, repo_name: str) -> Dict[str, Any]:
        """
        Ensure a tenant's infrastructure repo exists; if not, run the full
        provisioning flow (`provision`) so the repo is scaffolded identically
        to first-time org creation — clone source infra, tenant env files,
        initial commit, pipeline webhook secrets, `infrastructure_mst` record.

        Subdomain is derived from the repo name by stripping the
        `-infrastructure` suffix, matching the convention in `provision()`.
        Returns early with status=exists if the repo is already there.
        """
        token = await self._get_github_token()
        base_url = settings.github_base_url.rstrip("/")

        if await GitHubIntegration.repo_exists(token, base_url, owner, repo_name):
            return {"status": "exists", "owner": owner, "repo": repo_name}

        if not repo_name.endswith("-infrastructure"):
            self.logger.warning(
                f"[INFRA] Repo {owner}/{repo_name} missing but name doesn't follow "
                "'<subdomain>-infrastructure' convention — cannot auto-provision"
            )
            return {"status": "skipped", "reason": "unknown-naming-convention"}

        subdomain = repo_name[: -len("-infrastructure")]
        self.logger.info(
            f"[INFRA] Infra repo {owner}/{repo_name} missing — auto-provisioning "
            f"tenant '{subdomain}' (same flow as org creation)"
        )
        result = await self.provision(subdomain, github_org="", paas=True)
        result["auto_provisioned"] = True
        return result

    async def provision_infrastructure(self, subdomain: str, user_email: str) -> Dict[str, Any]:
        """
        Unified infrastructure provisioning entry point.

        Tier 1 — Enterprise On-Prem (in _MOCK_CANVAS_DATA):
            They manage their own infra. Return success immediately.

        Tier 2 — PaaS Shared (everyone else):
            Create infra repo with scaffold (no base/eks terragrunt),
            create infra_mst record (ACTIVE).
        """
        enterprise_tenants = _MOCK_CANVAS_DATA.keys()

        if subdomain in enterprise_tenants:
            # Tier 1: Enterprise On-Prem — they manage their own infra
            self.logger.info(f"[INFRA] Enterprise tenant '{subdomain}' — infra managed externally")
            return {
                "status": "success",
                "message": f"Enterprise tenant '{subdomain}' — infrastructure managed externally",
            }
        else:
            # Tier 2: PaaS Shared — create infra repo + shared cluster record
            self.logger.info(f"[INFRA] PaaS tenant '{subdomain}' — provisioning infra repo + shared cluster record")
            return await self.provision(subdomain, github_org="", paas=True)

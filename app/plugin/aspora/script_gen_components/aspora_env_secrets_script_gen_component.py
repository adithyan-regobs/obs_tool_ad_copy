"""
Aspora Env Secrets Script Generation Component

Creates secrets.json for ECS services when missing.
"""

import json
import logging
import re
from typing import Dict, Any

from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.timing import log_timing

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

VANCE_ASPORA_TENANTS = {"vance", "aspora"}

FALCON_SERVICES = {
    "falcon-api", "falcon-consumer", "falcon-worker",
    "falcon-api-dev-service", "falcon-consumer-dev-service", "falcon-worker-dev-service",
}

AWS_REGION_SHORT_NAMES = {
    "ap-south-1": "mumbai",
    "eu-west-2": "london",
}

DUMMY_SECRETS_MAPPING = {
    "core-stage-mumbai": "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFkIl9tlXjOZg4B+B/K1KEzAAAAaDBmBgkqhkiG9w0BBwagWTBXAgEAMFIGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMzWDqQ33SavsJVpWrAgEQgCWmD05d1AdGjJNDHWYu47oTSmKNSjmBq5mtO3wvSr4K3R87m2Wm",
    "core-prod-london": "AQICAHjAfH0s/mY6tiB3hNy775bFAf/KDHt8LHd/BtH2djiKMwFyHm6O2Fz3lQVzZFYCmbdTAAAAZzBlBgkqhkiG9w0BBwagWDBWAgEAMFEGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMFjYCb4gR9ohAvbPjAgEQgCTkTO6TL77eQKUqsnYPXfI7ZzgmWsj8j8+FSIw66LlqOQL7rSo=",
    "core-prod-mumbai": "AQICAHjwEJsUl4NAwm2LVgKaxe8VI3nWVsPlMMDL1tFPfl/s8AEC3BEn8e30CZ4ePxDNQQEJAAAAZDBiBgkqhkiG9w0BBwagVTBTAgEAME4GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMbYOU8R2SG14doKjNAgEQgCHvoZ+w44pHH+KGb3Nnf3PUZh/GUws9nbC/i88mTTfrb7Q=",
    "falcon-stage-mumbai": "AQICAHh2yyxMp2H7t1xSeylfRX58eWVqg3VEmGGcucb/1FjL/QEppPAzGbuMzICv1mwXzuwuAAAAaDBmBgkqhkiG9w0BBwagWTBXAgEAMFIGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMO6LylcdFd/Z3N+ZPAgEQgCVayzX3ATEDe7EM15BNDJz69vBACEbAgeGj2+z7lnU/oHxW55HV",
    "falcon-prod-london": "AQICAHjkAuvo1uup+0hHL4HC7ksdPvZrhOPVxWCxL+tKsNxC6QEODIbe+ugm7HsIl5UFITtQAAAAZDBiBgkqhkiG9w0BBwagVTBTAgEAME4GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM78h7n2UMikXuDpuVAgEQgCGvBGu7u6SztWKqYQ15IyKyoCRiUcHt+CMDkrVff7LmMRU=",
}


def _find_staged_entry(workflow_context, repo: str, base_branch: str, file_path: str):
    if not workflow_context:
        return None
    for entry in workflow_context.staged_files:
        if (
            entry.get("repo") == repo
            and entry.get("base_branch") == base_branch
            and entry.get("file_path") == file_path
        ):
            return entry
    return None


def _upsert_staged_entry(
    workflow_context,
    repo: str,
    base_branch: str,
    feature_branch: str,
    file_path: str,
    content: str,
    queue_id,
    script_gen_key,
):
    if not workflow_context:
        return
    entry = _find_staged_entry(workflow_context, repo, base_branch, file_path)
    if entry:
        entry["content"] = content
        entry["feature_branch"] = feature_branch or entry.get("feature_branch")
        entry["queue_id"] = queue_id
        entry["script_gen_key"] = script_gen_key
        return
    workflow_context.staged_files.append({
        "repo": repo,
        "base_branch": base_branch,
        "feature_branch": feature_branch,
        "file_path": file_path,
        "content": content,
        "queue_id": queue_id,
        "script_gen_key": script_gen_key,
    })


def _append_commit_message(workflow_context, repo: str, base_branch: str, message: str) -> None:
    if not workflow_context or not message:
        return
    key = f"{repo}|||{base_branch}"
    existing = workflow_context.commit_messages.get(key, "")
    if existing:
        workflow_context.commit_messages[key] = f"{existing}\n{message}"
    else:
        workflow_context.commit_messages[key] = message


class AsporaEnvSecretsScriptGenComponent:
    """Component for generating env secrets.json files."""

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.WARNING)
        self.repository = repository

    async def generate(
        self,
        tenant: str,
        repository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = True,
        db=None,
    ) -> str:
        config_snapshot = queue_dict.get("config_snapshot") or {}
        service_name = config_snapshot.get("service_name")
        product_name = config_snapshot.get("product_name") or config_snapshot.get("application_name")
        environment = config_snapshot.get("environment") or queue_dict.get("environment") or "dev"
        region = config_snapshot.get("region") or config_snapshot.get("geo_loc_mst_code") or queue_dict.get("region")

        if not service_name:
            raise ValueError("service_name is required for env secrets generation")
        if not product_name:
            raise ValueError("product_name is required for env secrets generation")

        service_lower = service_name.lower()
        skip_env_files = (
            product_name.lower() == "falcon"
            and service_lower in FALCON_SERVICES
            and (tenant or "").lower() in VANCE_ASPORA_TENANTS
        )
        if skip_env_files:
            logger.warning(
                "Skipping env secrets creation for Falcon service: product=%s, service=%s, tenant=%s",
                product_name, service_name, tenant
            )
            if tenant and file_location and workflow_context and queue_dict.get("id"):
                script_gen_key = file_location.script_gen_key
                workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key] = {
                    "original_content": "",
                    "preview_content": ""
                }
            return ""

        if not region:
            raise ValueError("region is required for env secrets generation")

        region = self._get_aws_region_from_geo_loc(region)

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context,
                file_location.repo,
                base_branch,
                file_location.file_path
            )
        if cached_entry:
            existing_file = {
                "exists": True,
                "content": cached_entry.get("content")
            }
        else:
            fetch_context = f"repo={repo} branch={feature_branch} path={file_location.file_path}"
            with log_timing(self.logger, f"{component_name}.fetch_content", context=fetch_context):
                existing_file = await GitOpsHandler.get_content(
                    db=db,
                    tenant=tenant,
                    owner=owner,
                    repo=repo,
                    file_path=file_location.file_path,
                    branch=feature_branch
                )

        if existing_file.get("status") == "error":
            raise ValueError(f"GitOps get_content failed: {existing_file.get('error')}")

        if existing_file.get("exists"):
            content = existing_file.get("content", "")
        else:
            gen_context = f"path={file_location.file_path} mode=create"
            with log_timing(self.logger, f"{component_name}.script_generation", context=gen_context):
                encrypted_value = self._get_dummy_secret_value(
                    product=product_name,
                    environment=environment,
                    region=region,
                    tenant=tenant
                )
                content = json.dumps({"AWS_REGION": encrypted_value}, indent=2) + "\n"

            if tenant and workflow_context and not workflow_context.skip_commit:
                _upsert_staged_entry(
                    workflow_context=workflow_context,
                    repo=file_location.repo,
                    base_branch=base_branch,
                    feature_branch=feature_branch,
                    file_path=file_location.file_path,
                    content=content,
                    queue_id=queue_dict.get("id"),
                    script_gen_key=file_location.script_gen_key
                )
                queue_label = queue_dict.get("code") or queue_dict.get("id")
                if queue_label:
                    commit_line = f"{queue_label}: {file_location.script_gen_key} -> {file_location.file_path}"
                else:
                    commit_line = f"{file_location.script_gen_key} -> {file_location.file_path}"
                _append_commit_message(
                    workflow_context,
                    file_location.repo,
                    base_branch,
                    commit_line
                )

        if upload_to_s3:
            identifier = config_snapshot.get("identifier") or service_name
            key_suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", file_location.file_path.strip("/"))
            original_s3_key = f"env-files/{identifier}/{key_suffix}"
            result = await FileManagerHandler.upload_file(
                key=original_s3_key,
                content=content,
                content_type="application/json"
            )
            if repository and queue_dict.get("code"):
                update_payload = json.dumps({
                    "original_s3_key": original_s3_key,
                    "preview": original_s3_key,
                    "location": result.get("location")
                })
                await repository.update_artifact_s3_key(queue_dict.get("code"), update_payload)

        if tenant and file_location and workflow_context and queue_dict.get("id"):
            script_gen_key = file_location.script_gen_key
            workflow_context.script_gen_responses[queue_dict.get("id")][script_gen_key] = {
                "original_content": content,
                "preview_content": content
            }

        return content

    def _get_dummy_secret_value(
        self,
        product: str,
        environment: str,
        region: str,
        tenant: str = ""
    ) -> str:
        region_short = AWS_REGION_SHORT_NAMES.get(region, region)
        product_lower = product.lower()
        env_lower = environment.lower()
        if tenant.lower() in VANCE_ASPORA_TENANTS and env_lower == "staging":
            env_normalized = "stage"
        else:
            env_normalized = env_lower

        exact_key = f"{product_lower}-{env_normalized}-{region_short}"
        if exact_key in DUMMY_SECRETS_MAPPING:
            return DUMMY_SECRETS_MAPPING[exact_key]

        env_region_suffix = f"-{env_normalized}-{region_short}"
        for key, value in DUMMY_SECRETS_MAPPING.items():
            if key.endswith(env_region_suffix):
                return value

        region_suffix = f"-{region_short}"
        for key, value in DUMMY_SECRETS_MAPPING.items():
            if key.endswith(region_suffix):
                return value

        return next(iter(DUMMY_SECRETS_MAPPING.values()))

    @staticmethod
    def _get_aws_region_from_geo_loc(geo_loc: str) -> str:
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
        return mapping.get(geo_loc.lower(), geo_loc)

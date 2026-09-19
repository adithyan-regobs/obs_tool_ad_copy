"""
Default Redis (ElastiCache) Terragrunt Script Generation Component

Generates a terragrunt.hcl for Redis provisioning.
The generated file is committed to the tenant's infra repo for
disaster recovery and GitOps record keeping.

Template: templates/terragrunt/default/redis_terragrunt.hcl

Variables replaced:
  - node_type (instance class, e.g. cache.t4g.micro)
  - engine_version (Redis engine version, e.g. 7.1)
  - port (default 6379)
"""

import os
import re
import logging
from typing import Optional

import aiofiles

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "terragrunt", "default", "redis_terragrunt.hcl"
)


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
    queue_id: Optional[int],
    script_gen_key: Optional[str],
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


class DefaultRedisScriptGenComponent:
    """
    Generates Redis (ElastiCache) terragrunt.hcl from the default template.

    Replaces node_type and optionally inserts engine_version, num_cache_nodes,
    port, multi_az_enabled settings.
    The generated content is staged for git commit to the infra repo.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def generate(
        self,
        tenant: str,
        repository,
        file_location,
        queue_dict: dict,
        workflow_context,
        upload_to_s3: bool = False,
        db=None,
    ) -> str:
        config_snapshot = queue_dict.get("config_snapshot") or {}

        from app.core.config import settings

        identifier = config_snapshot.get("redis_cluster_name") or config_snapshot.get("identifier", "")
        node_type = config_snapshot.get("node_type") or "cache.t4g.micro"
        engine_version = config_snapshot.get("engine_version")
        port = config_snapshot.get("port")

        # Network defaults from config
        vpc_id = settings.onboarding_default_vpc_id
        subnet_ids = [s.strip() for s in settings.onboarding_default_subnet_ids.split(",") if s.strip()]
        vpc_cidr = settings.onboarding_default_vpc_cidr

        # Load template
        async with aiofiles.open(_TEMPLATE_PATH, "r") as f:
            content = await f.read()

        # Replace vpc_id
        content = re.sub(
            r'(vpc_id\s*=\s*)"[^"]*"',
            f'\\1"{vpc_id}"',
            content,
            count=1,
        )

        # Replace subnets
        subnets_formatted = ", ".join(f'"{s}"' for s in subnet_ids)
        content = re.sub(
            r'(subnets\s*=\s*)\[[^\]]*\]',
            f'\\1[{subnets_formatted}]',
            content,
            count=1,
        )

        # Replace allowed_cidr_blocks with VPC CIDR
        content = re.sub(
            r'(allowed_cidr_blocks\s*=\s*)\[[^\]]*\]',
            f'\\1["{vpc_cidr}"]',
            content,
            count=1,
        )

        # Replace node_type
        content = re.sub(
            r'(node_type\s*=\s*)"[^"]*"',
            f'\\1"{node_type}"',
            content,
            count=1,
        )

        # Replace engine_version if provided
        if engine_version is not None:
            content = re.sub(
                r'(engine_version\s*=\s*)"[^"]*"',
                f'\\1"{engine_version}"',
                content,
                count=1,
            )

        # Replace port if provided
        if port is not None:
            content = re.sub(
                r'(port\s*=\s*)\d+',
                f'\\g<1>{port}',
                content,
                count=1,
            )

        # Stage for git commit
        if workflow_context and file_location.repo and file_location.file_path:
            base_branch = file_location.base_branch or ""
            _upsert_staged_entry(
                workflow_context=workflow_context,
                repo=file_location.repo,
                base_branch=base_branch,
                feature_branch=file_location.feature_branch or "",
                file_path=file_location.file_path,
                content=content,
                queue_id=queue_dict.get("id"),
                script_gen_key=file_location.script_gen_key,
            )

            queue_label = queue_dict.get("code") or queue_dict.get("id")
            msg = f"{queue_label}: add Redis cluster {identifier}"
            _append_commit_message(workflow_context, file_location.repo, base_branch, msg)

        # Store preview
        if queue_dict.get("id") and workflow_context:
            workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                "original_content": content,
                "preview_content": content,
            }

        return content

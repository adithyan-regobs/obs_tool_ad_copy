"""
Aspora EKS Rollback Script Gen Component

Handles the `k8s_rollback` script_gen_key.

Fetches the current env values.yaml from the k8s-manifests feature branch and
replaces only the `image.tag` line with the `target_sha` from config_snapshot.
All other values (resources, keda, ingress, etc.) are left unchanged.
"""

import logging
import re

from app.handlers.gitops_handler import GitOpsHandler
from app.utils.timing import log_timing


class AsporaEksRollbackScriptGenComponent:

    def __init__(self, repository=None):
        self.logger = logging.getLogger(__name__)
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
        config_snapshot = dict(queue_dict.get("config_snapshot") or {})
        target_sha = config_snapshot.get("target_sha") or config_snapshot.get("image_tag")
        if not target_sha:
            raise ValueError("k8s_rollback: target_sha is required in config_snapshot")

        repo_parts = file_location.repo.split("/")
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""

        with log_timing(self.logger, "AsporaEksRollbackScriptGenComponent.fetch_content",
                        context=f"repo={repo} branch={feature_branch} path={file_location.file_path}"):
            existing_file = await GitOpsHandler.get_content(
                db=db,
                tenant=tenant,
                owner=owner,
                repo=repo,
                file_path=file_location.file_path,
                branch=feature_branch,
            )

        if existing_file.get("status") == "error":
            raise ValueError(f"k8s_rollback: failed to fetch {file_location.file_path}: {existing_file.get('error')}")

        if not existing_file.get("exists") or not existing_file.get("content"):
            raise ValueError(
                f"k8s_rollback: {file_location.file_path} does not exist on branch {feature_branch!r}. "
                "Cannot roll back a service that has never been deployed."
            )

        content = self._replace_image_tag(existing_file["content"], target_sha)

        if workflow_context and not workflow_context.skip_commit:
            self._upsert_staged(workflow_context, file_location, base_branch, content, queue_dict)
            queue_label = queue_dict.get("code") or queue_dict.get("id")
            commit_line = (
                f"{queue_label}: k8s_rollback -> {file_location.file_path} (sha={target_sha[:9]})"
                if queue_label
                else f"k8s_rollback -> {file_location.file_path} (sha={target_sha[:9]})"
            )
            self._append_commit_message(workflow_context, file_location.repo, base_branch, commit_line)

        if queue_dict.get("id") and workflow_context:
            workflow_context.script_gen_responses[queue_dict["id"]]["k8s_rollback"] = {
                "original_content": content,
                "preview_content": content,
            }

        return content

    @staticmethod
    def _replace_image_tag(content: str, target_sha: str) -> str:
        # Replace `tag: <anything>` under the `image:` block.
        # The YAML structure always has `image:\n  tag: <sha>` as the first two lines.
        new_content = re.sub(
            r"^(\s*tag:\s*).*$",
            lambda m: f"{m.group(1)}{target_sha}",
            content,
            flags=re.MULTILINE,
        )
        return new_content

    @staticmethod
    def _upsert_staged(workflow_context, file_location, base_branch, content, queue_dict):
        entry = None
        for e in workflow_context.staged_files:
            if (
                e.get("repo") == file_location.repo
                and e.get("base_branch") == base_branch
                and e.get("file_path") == file_location.file_path
            ):
                entry = e
                break
        if entry:
            entry["content"] = content
            entry["feature_branch"] = file_location.feature_branch or entry.get("feature_branch")
            entry["queue_id"] = queue_dict.get("id")
            entry["script_gen_key"] = "k8s_rollback"
        else:
            workflow_context.staged_files.append({
                "repo": file_location.repo,
                "base_branch": base_branch,
                "feature_branch": file_location.feature_branch,
                "file_path": file_location.file_path,
                "content": content,
                "queue_id": queue_dict.get("id"),
                "script_gen_key": "k8s_rollback",
            })

    @staticmethod
    def _append_commit_message(workflow_context, repo: str, base_branch: str, message: str) -> None:
        if not message:
            return
        key = f"{repo}|||{base_branch}"
        existing = workflow_context.commit_messages.get(key, "")
        workflow_context.commit_messages[key] = f"{existing}\n{message}" if existing else message

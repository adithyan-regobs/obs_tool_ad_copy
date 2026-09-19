"""
Default Dockerfile Script Generation Component

Generates a Dockerfile from language-specific templates and commits it to the
service repository. Supports Java (Gradle/Maven), Go, Python, and Node.js.

Template resolution:
  templates/dockerfiles/java-standard.Dockerfile
  templates/dockerfiles/go-standard.Dockerfile
  templates/dockerfiles/python-standard.Dockerfile
  templates/dockerfiles/nodejs-standard.Dockerfile

Only runs when generate_dockerfile=True in config_snapshot.
"""

import json
import logging
import os
import aiofiles
from typing import Optional, List, Dict

from app.handlers.file_manager_handler import FileManagerHandler
from app.handlers.gitops_handler import GitOpsHandler
from app.utils.github_sync_helpers import should_skip_commit
from app.utils.timing import log_timing
from app.utils.language_helpers import is_java_language, is_go_language, is_python_language

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "templates", "dockerfiles"
)

_DEFAULT_VERSIONS = {
    "java": "17",
    "go": "1.24",
    "python": "3.11",
    "nodejs": "20",
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


def _upsert_staged_entry(workflow_context, repo, base_branch, feature_branch, file_path, content, queue_id, script_gen_key):
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
    workflow_context.commit_messages[key] = f"{existing}\n{message}" if existing else message


def _is_nodejs(language_name: str) -> bool:
    lang = (language_name or "").lower()
    return "node" in lang


class DefaultDockerfileScriptGenComponent:
    """
    Generates a Dockerfile from the standard language-specific template.
    Supports Java, Go, Python, and Node.js.
    """

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

        service_name = config_snapshot.get("service_name", "")
        if service_name.lower().endswith("-service"):
            service_name = service_name[:-8]

        language_name = config_snapshot.get("language_name") or config_snapshot.get("language") or "java"
        language_version = config_snapshot.get("language_version") or config_snapshot.get("version") or ""
        identifier = config_snapshot.get("identifier") or service_name
        environment = config_snapshot.get("environment") or queue_dict.get("environment") or "stage"
        port = config_snapshot.get("port") or config_snapshot.get("container_port") or ""
        xms = config_snapshot.get("xms")
        xmx = config_snapshot.get("xmx")
        build_args = config_snapshot.get("build_args")
        go_config_path = config_snapshot.get("go_config_path")
        use_aws_secrets = config_snapshot.get("use_aws_secrets", False)

        repo_parts = file_location.repo.split('/')
        owner = repo_parts[0] if len(repo_parts) > 1 else None
        repo = repo_parts[1] if len(repo_parts) > 1 else file_location.repo
        feature_branch = file_location.feature_branch
        base_branch = file_location.base_branch or file_location.target_branch or ""
        component_name = self.__class__.__name__

        # ── Check staged cache ───────────────────────────────────────────────
        cached_entry = None
        if workflow_context and not workflow_context.skip_commit:
            cached_entry = _find_staged_entry(
                workflow_context, file_location.repo, base_branch, file_location.file_path
            )
        if cached_entry:
            existing_file = {"exists": True, "content": cached_entry.get("content")}
        else:
            fetch_context = f"repo={repo} branch={feature_branch} path={file_location.file_path}"
            with log_timing(logger, f"{component_name}.fetch_content", context=fetch_context):
                existing_file = await GitOpsHandler.get_content(
                    db=db, tenant=tenant, owner=owner, repo=repo,
                    file_path=file_location.file_path, branch=feature_branch,
                )

        # ── Generate Dockerfile from template ────────────────────────────────
        gen_context = f"path={file_location.file_path} service={service_name} lang={language_name}"
        with log_timing(logger, f"{component_name}.script_generation", context=gen_context):
            content = await self._generate_from_template(
                language_name=language_name,
                language_version=language_version,
                service_name=service_name,
                port=port,
                xms=xms,
                xmx=xmx,
                build_args=build_args,
                go_config_path=go_config_path,
                use_aws_secrets=use_aws_secrets,
            )

        # ── Upload to S3 ─────────────────────────────────────────────────────
        if upload_to_s3:
            try:
                original_s3_key = f"default/dockerfile/{identifier}.Dockerfile"
                await FileManagerHandler.upload_file(
                    key=original_s3_key, content=content, content_type="text/plain",
                )
                repo_for_db = repository or self.repository
                if repo_for_db and queue_dict.get("code"):
                    await repo_for_db.update_artifact_s3_key(
                        queue_dict["code"],
                        json.dumps({"original_s3_key": original_s3_key, "preview": original_s3_key}),
                    )
            except Exception as exc:
                logger.error("Failed to upload Dockerfile to S3: %s", exc, exc_info=True)
                raise

        # ── Stage for git commit ─────────────────────────────────────────────
        if tenant and file_location and workflow_context:
            if not workflow_context.skip_commit:
                skip_commit = False
                existing_content = existing_file.get("content")
                if existing_file.get("exists") and existing_content:
                    skip_commit = should_skip_commit(existing_content, content)
                if not skip_commit:
                    _upsert_staged_entry(
                        workflow_context=workflow_context,
                        repo=file_location.repo,
                        base_branch=base_branch,
                        feature_branch=feature_branch,
                        file_path=file_location.file_path,
                        content=content,
                        queue_id=queue_dict.get("id"),
                        script_gen_key=file_location.script_gen_key,
                    )
                    queue_label = queue_dict.get("code") or queue_dict.get("id")
                    commit_line = (
                        f"{queue_label}: {file_location.script_gen_key} -> {file_location.file_path}"
                        if queue_label
                        else f"{file_location.script_gen_key} -> {file_location.file_path}"
                    )
                    _append_commit_message(workflow_context, file_location.repo, base_branch, commit_line)

            if queue_dict.get("id"):
                workflow_context.script_gen_responses[queue_dict["id"]][file_location.script_gen_key] = {
                    "original_content": content,
                    "preview_content": content,
                }

        return content

    async def _generate_from_template(
        self,
        language_name: str,
        language_version: str,
        service_name: str,
        port: str = "",
        xms: Optional[int] = None,
        xmx: Optional[int] = None,
        build_args: Optional[List[Dict]] = None,
        go_config_path: Optional[str] = None,
        use_aws_secrets: bool = False,
    ) -> str:
        if is_java_language(language_name):
            return await self._generate_java(
                version=language_version or _DEFAULT_VERSIONS["java"],
                xms=xms, xmx=xmx, build_args=build_args,
            )
        if is_go_language(language_name):
            return await self._generate_go(
                port=port or "8080",
                go_config_path=go_config_path,
                use_aws_secrets=use_aws_secrets,
                build_args=build_args,
            )
        if is_python_language(language_name):
            return await self._generate_python(
                version=language_version or _DEFAULT_VERSIONS["python"],
                port=port or "8000",
                build_args=build_args,
            )
        if _is_nodejs(language_name):
            return await self._generate_nodejs(
                version=language_version or _DEFAULT_VERSIONS["nodejs"],
                port=port or "3000",
                build_args=build_args,
            )
        raise ValueError(f"Unsupported language for Dockerfile generation: {language_name}")

    def _build_args_block(self, build_args: Optional[List[Dict]], reserved: Optional[set] = None) -> str:
        if not build_args:
            return ""
        reserved = reserved or set()
        seen = set()
        lines = []
        for arg in build_args:
            name = (arg.get("name") or arg.get("key") or "").strip().upper()
            if name and name not in reserved and name not in seen:
                seen.add(name)
                lines.append(f"ARG {name}")
        return "\n".join(lines) + "\n" if lines else ""

    async def _generate_java(self, version: str, xms=None, xmx=None, build_args=None) -> str:
        template_path = os.path.join(_TEMPLATE_DIR, "java-standard.Dockerfile")
        async with aiofiles.open(template_path) as f:
            content = await f.read()

        content = content.replace("{{JDK_VERSION}}", version)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", self._build_args_block(build_args, {"JAR_FILE", "PROFILE"}))

        # Replace DATADOG_BLOCK with plain JAVA_TOOL_OPTIONS
        opts_parts = []
        if xms:
            opts_parts.append(f"-Xms{xms}m")
        if xmx:
            opts_parts.append(f"-Xmx{xmx}m")
        opts_parts.append("-Dlogging.level.root=info")
        java_tool_line = f'ENV JAVA_TOOL_OPTIONS="{" ".join(opts_parts)}"\n\n'
        content = content.replace("{{DATADOG_BLOCK}}\n", java_tool_line)

        return content

    async def _generate_go(self, port: str, go_config_path=None, use_aws_secrets=False, build_args=None) -> str:
        template_path = os.path.join(_TEMPLATE_DIR, "go-standard.Dockerfile")
        async with aiofiles.open(template_path) as f:
            content = await f.read()

        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", self._build_args_block(build_args))

        if use_aws_secrets:
            aws_block = (
                "ARG AWS_SECRETS_MANAGER_NAME\nARG CONFIG_ENV\n"
                'RUN if [ -z "$CONFIG_ENV" ]; then echo "ERROR: CONFIG_ENV build arg is required" && exit 1; fi\n'
                "ENV AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID\n"
                "ENV AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY\n"
                "ENV AWS_REGION=$AWS_REGION\n"
                "ENV AWS_SECRETS_MANAGER_NAME=$AWS_SECRETS_MANAGER_NAME\n"
                "ENV CONFIG_ENV=$CONFIG_ENV\n"
            )
            content = content.replace("{{AWS_SECRETS_BLOCK}}\n", aws_block)
        else:
            content = content.replace("{{AWS_SECRETS_BLOCK}}\n", "")

        if go_config_path:
            config_type = "json" if go_config_path.endswith(".json") else "yaml"
            content = content.replace("{{CONFIG_PATH}}", f"/app/{go_config_path}")
            content = content.replace("{{CONFIG_TYPE}}", config_type)
            content = content.replace("{{CONFIG_COPY_LINE}}", f"COPY {go_config_path} /app/{go_config_path}")
        else:
            content = content.replace("{{CONFIG_PATH}}", "/app/configs/config.json")
            content = content.replace("{{CONFIG_TYPE}}", "json")
            content = content.replace("{{CONFIG_COPY_LINE}}", "")

        content = content.replace("{{PORT}}", port)
        return content

    async def _generate_python(self, version: str, port: str, build_args=None) -> str:
        template_path = os.path.join(_TEMPLATE_DIR, "python-standard.Dockerfile")
        async with aiofiles.open(template_path) as f:
            content = await f.read()

        content = content.replace("{{PYTHON_VERSION}}", version)
        content = content.replace("{{PORT}}", port)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", self._build_args_block(build_args))
        return content

    async def _generate_nodejs(self, version: str, port: str, build_args=None) -> str:
        template_path = os.path.join(_TEMPLATE_DIR, "nodejs-standard.Dockerfile")
        async with aiofiles.open(template_path) as f:
            content = await f.read()

        content = content.replace("{{NODE_VERSION}}", version)
        content = content.replace("{{PORT}}", port)
        content = content.replace("{{CUSTOM_BUILD_ARGS}}\n", self._build_args_block(build_args))
        return content

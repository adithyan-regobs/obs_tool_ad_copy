"""
DevLift Service Onboarding MCP Server (FastMCP version).

Single tool: HostService
Uses FastMCP with Pydantic models, state tracking, fuzzy matching, and hallucination detection.
"""
import difflib
import json
import os
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import BaseModel, Field, ValidationError
from mcp.server.fastmcp import FastMCP

from app.infra_chat_agent_with_tools.mcp_server.eks.eks_models import TOOL_MODELS

# Import generators from original server
import sys
sys.path.insert(0, os.path.dirname(__file__))
from app.infra_chat_agent_with_tools.mcp_server.eks.eks_generators import (
    sanitize_name,
    get_default_port,
    generate_dockerfile,
    generate_workflow,
    generate_kustomization_yaml,
    generate_deployment_yaml,
    generate_service_yaml,
    generate_ingress_yaml,
    generate_namespace_yaml,
    generate_serviceaccount_yaml,
)
from app.infra_chat_agent_with_tools.mcp_server.eks.eks_deployment import (
    check_kubectl_ready,
    deploy_to_k8s,
)
from app.infra_chat_agent_with_tools.mcp_server.eks.eks_status_monitoring import (
    get_alb_url_for_service,
    monitor_github_deployment_status,
    trigger_github_workflow,
)
from app.infra_chat_agent_with_tools.mcp_server.eks.eks_config import (
    DEFAULT_EKS_CLUSTER_NAME,
    DEFAULT_ALB_SCHEME,
    DEFAULT_ECR_REGISTRY,
    DEFAULT_IMAGE_TAG,
    DEFAULT_CPU_REQUESTED,
    DEFAULT_CPU_LIMIT,
    DEFAULT_MEMORY_REQUESTED,
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_REPLICA_COUNT,
    DEFAULT_HEALTH_ENDPOINT,
    DEVLIFT_BASE_DOMAIN,
    DEFAULT_SHARED_ENV,
)


# ─── Tool Executor ───

def execute_host_service(params: BaseModel) -> str:
    """Execute host_service tool."""
    d = params.model_dump()

    # Apply defaults
    service_name = sanitize_name(d["service_name"])

    # Construct language string with build tool for Java
    language = d["language"]
    if language.lower() == "java":
        build_tool = d.get("build_tool") or "gradle"  # Default to gradle
        language = f"java-{build_tool}"

    port = d.get("port")
    if port is None:
        port = get_default_port(language)

    # Resolve namespace, ALB group, and ingress hostname.
    # When tenant_code is provided the service joins the shared namespace+ALB
    # that was pre-provisioned at signup.  Without it we fall back to the
    # legacy per-service namespace for backwards compatibility.
    tenant_code = d.get("tenant_code")
    env = d["environment"]
    service_path = None  # Let generator handle None -> "/" default

    if tenant_code:
        shared_env = DEFAULT_SHARED_ENV  # always "stage" for shared ALB group + hostname suffix
        namespace = f"{tenant_code}-ns"
        alb_group_name = f"{tenant_code}-{shared_env}"
        ingress_hostname = f"{service_name}-{tenant_code}-{shared_env}.apps.{DEVLIFT_BASE_DOMAIN}"
        # Always include namespace.yaml — kubectl apply is idempotent, so this
        # is a no-op if the namespace already exists (from the bootstrap pipeline)
        # but creates it safely if the bootstrap hasn't run yet.
        include_namespace_yaml = True
    else:
        namespace = service_name
        alb_group_name = None
        ingress_hostname = None
        include_namespace_yaml = True

    # Determine Dockerfile path
    dockerfile_path = d.get("dockerfile_path") or "Dockerfile"
    if not d["generate_dockerfile"] and not d.get("dockerfile_path"):
        dockerfile_path = "Dockerfile"  # Default to "Dockerfile" if user has existing one

    # Generate files
    files = [
        {"path": f".github/workflows/deploy-{service_name}-{env}.yml", "content": generate_workflow(service_name, language, d["version"], env, d["branch"], DEFAULT_EKS_CLUSTER_NAME, dockerfile_path)},
    ]

    if d["generate_dockerfile"]:
        files.append({"path": dockerfile_path, "content": generate_dockerfile(language, port)})

    # Kustomize manifests
    kustomize_base_dir = "k8s/base"

    if include_namespace_yaml:
        files.append({
            "path": f"{kustomize_base_dir}/namespace.yaml",
            "content": generate_namespace_yaml(namespace, env)
        })

    files.append({
        "path": f"{kustomize_base_dir}/serviceaccount.yaml",
        "content": generate_serviceaccount_yaml(service_name, namespace, env)
    })

    files.append({
        "path": f"{kustomize_base_dir}/kustomization.yaml",
        "content": generate_kustomization_yaml(
            service_name, env, namespace,
            tenant_code=tenant_code or "",
            include_namespace=include_namespace_yaml,
        )
    })

    files.append({
        "path": f"{kustomize_base_dir}/deployment.yaml",
        "content": generate_deployment_yaml(
            service_name=service_name,
            namespace=namespace,
            port=port,
            cpu_requested=DEFAULT_CPU_REQUESTED,
            cpu_limit=DEFAULT_CPU_LIMIT,
            memory_requested=DEFAULT_MEMORY_REQUESTED,
            memory_limit=DEFAULT_MEMORY_LIMIT,
            replica_count=DEFAULT_REPLICA_COUNT,
            health_endpoint=DEFAULT_HEALTH_ENDPOINT,
            ecr_registry=DEFAULT_ECR_REGISTRY,
            image_tag=DEFAULT_IMAGE_TAG
        )
    })

    files.append({
        "path": f"{kustomize_base_dir}/service.yaml",
        "content": generate_service_yaml(service_name, namespace, port)
    })

    files.append({
        "path": f"{kustomize_base_dir}/ingress.yaml",
        "content": generate_ingress_yaml(
            service_name=service_name,
            namespace=namespace,
            port=port,
            service_path=service_path,
            health_endpoint=DEFAULT_HEALTH_ENDPOINT,
            alb_schema=DEFAULT_ALB_SCHEME,
            alb_group_name=alb_group_name,
            ingress_hostname=ingress_hostname,
        )
    })

    # Separate files into two categories
    files_to_commit = []
    k8s_manifests = []

    for file in files:
        if file["path"].startswith("k8s/base/"):
            k8s_manifests.append(file)
        else:
            files_to_commit.append(file)

    # Check kubectl readiness
    kubectl_check = check_kubectl_ready(DEFAULT_EKS_CLUSTER_NAME)
    if not kubectl_check["ready"]:
        return json.dumps({
            "service_name": service_name,
            "environment": env,
            "port": port,
            "namespace": namespace,
            "files_to_commit": files_to_commit,
            "k8s_manifests": k8s_manifests,
            "deployment": {
                "success": False,
                "error": kubectl_check["error"],
                "message": "kubectl is not configured. Please configure kubectl before deployment."
            },
            "message": f"Generated files for {service_name}, but could not deploy: {kubectl_check['error']}",
            "instructions": {
                "allowed_modifications": {
                    "Dockerfile": [
                        "Adjust EXPOSE port if application uses a different port",
                        "Modify ENTRYPOINT or CMD to match application entry point",
                        "Update WORKDIR paths if needed",
                        "Add application-specific environment variables",
                        "Change base image version if required"
                    ]
                }
            }
        }, indent=2)

    # Deploy to Kubernetes
    deployment_result = deploy_to_k8s(k8s_manifests, service_name, namespace)

    # Build next steps based on whether Dockerfile was generated
    next_steps = [
        "1. ✅ Cloud infrastructure is ready to host your application",
        "2. ⚠️  YOUR APPLICATION IS NOT DEPLOYED YET - Only the hosting environment is set up",
    ]

    if d["generate_dockerfile"]:
        next_steps.append("3. 📝 Generated files (Dockerfile and GitHub workflow) are ready")
    else:
        next_steps.append(f"3. 📝 Using your existing Dockerfile at '{dockerfile_path}'. GitHub workflow is ready")

    next_steps.extend([
        "4. 🔨 Commit and push these files to GitHub to trigger automatic build and deployment",
        f"5. 🌐 Your application will be accessible at: {deployment_result.get('alb_url', 'pending')}"
    ])

    # Return files in response for client to handle
    # (server-hosted MCP cannot write to client's local filesystem)
    response = {
        "service_name": service_name,
        "environment": env,
        "port": port,
        "namespace": namespace,
        "deployment": deployment_result,
        "alb_url": deployment_result.get("alb_url", "pending"),
        "files": files_to_commit,
        "dockerfile_info": {
            "generated": d["generate_dockerfile"],
            "path": dockerfile_path
        },
        "next_steps": next_steps,
        "instructions": {
            "commit_and_deploy": {
                "description": "Commit and push the generated files to trigger deployment",
                "steps": [
                    "Review the generated files (Dockerfile and GitHub workflow)",
                    "Add all files: git add -A",
                    "Commit with message: git commit -m 'Add deployment configuration for {service_name}'",
                    "Push to GitHub: git push",
                    "GitHub Actions will automatically build and deploy your application"
                ]
            },
            "allowed_modifications": {
                "Dockerfile": [
                    "Adjust EXPOSE port if application uses a different port",
                    "Modify ENTRYPOINT or CMD to match application entry point",
                    "Update WORKDIR paths if needed",
                    "Add application-specific environment variables",
                    "Change base image version if required"
                ]
            }
        },
        "message": f"✅ Cloud hosting environment is ready! {deployment_result.get('message', '')} {'Using your existing Dockerfile at ' + dockerfile_path if not d['generate_dockerfile'] else 'Dockerfile and GitHub workflow generated'}. ⚠️ IMPORTANT: Your application is NOT deployed yet - commit and push the files to GitHub to trigger automatic build and deployment."
    }

    return json.dumps(response, indent=2)


def execute_get_alb_url(params: BaseModel) -> str:
    """Execute get_alb_url tool."""
    d = params.model_dump()

    service_name = d["service_name"]
    namespace = d.get("namespace")

    # Call monitoring function
    result = get_alb_url_for_service(
        service_name=service_name,
        namespace=namespace
    )

    return json.dumps(result, indent=2)


async def execute_monitor_deployment(params: BaseModel) -> str:
    """Execute monitor_deployment_status tool."""
    d = params.model_dump()

    # Call monitoring function (async)
    result = await monitor_github_deployment_status(
        service_name=d["service_name"],
        github_repo=d["github_repo"],
        branch=d["branch"],
        environment=d["environment"],
        poll_interval=d.get("poll_interval", 10),
        max_duration=d.get("max_duration", 600)
    )

    return json.dumps(result, indent=2)


async def execute_trigger_build(params: BaseModel) -> str:
    """Execute trigger_build tool."""
    d = params.model_dump()

    # Call trigger function (async)
    result = await trigger_github_workflow(
        service_name=d["service_name"],
        github_repo=d["github_repo"],
        branch=d["branch"],
        environment=d["environment"]
    )

    return json.dumps(result, indent=2)


EXECUTORS = {
    "HostService": execute_host_service,
    "GetAlbUrl": execute_get_alb_url,
    "MonitorDeployment": execute_monitor_deployment,
    "TriggerBuild": execute_trigger_build,
}


# ─── Server State ───

_state: dict[str, Any] = {"tool_name": None, "valid": {}}


# ─── Helper Functions ───

def _get_enum_values(annotation) -> list[str] | None:
    """Extract enum values from a Literal type annotation."""
    if get_origin(annotation) is Literal:
        args = get_args(annotation)
        if args and all(isinstance(a, str) for a in args):
            return list(args)
    return None


def _get_field_annotation(field_name: str, model: type[BaseModel]):
    """Get the resolved annotation for a field from Pydantic's model_fields."""
    field_info = model.model_fields.get(field_name)
    if field_info is None:
        return None
    return field_info.annotation


def _is_bool_field(annotation) -> bool:
    """Check if the annotation is a bool type (including Optional[bool])."""
    if annotation is bool:
        return True
    return bool in get_args(annotation)


def _get_field_hint(field_name: str, model: type[BaseModel]) -> str:
    """Build a human-readable hint for a missing field."""
    field_info = model.model_fields.get(field_name)
    if not field_info:
        return ""

    hints = []
    if field_info.description:
        hints.append(field_info.description)

    annotation = _get_field_annotation(field_name, model)
    if annotation:
        enum_vals = _get_enum_values(annotation)
        if enum_vals:
            hints.append(f"Options: {enum_vals}")

    if _is_bool_field(annotation):
        hints.append("yes/no")

    return ". ".join(hints)


async def validate_params_structured(
    tool_name: str,
    new_params: dict,
    user_message: str,
) -> dict:
    """Validate params with hallucination detection and state tracking."""
    global _state

    model = TOOL_MODELS.get(tool_name)
    if model is None:
        return {"error": f"Unknown tool '{tool_name}'. Available: {list(TOOL_MODELS.keys())}"}

    # Reset if tool changed
    if tool_name != _state["tool_name"]:
        _state = {"tool_name": tool_name, "valid": {}}

    hallucinated = {}
    invalid = {}
    corrected = {}
    valid_new = {}
    msg_lower = user_message.lower()

    for pname, value in new_params.items():
        # Fuzzy match param names
        if pname not in model.model_fields:
            field_matches = difflib.get_close_matches(pname, list(model.model_fields.keys()), n=1, cutoff=0.6)
            if field_matches:
                corrected[pname] = {"original": pname, "corrected_to": field_matches[0]}
                pname = field_matches[0]
            else:
                invalid[pname] = {"value": value, "reason": f"Unknown parameter. Available: {list(model.model_fields.keys())}"}
                continue

        field_info = model.model_fields[pname]
        annotation = _get_field_annotation(pname, model)

        # Hallucination check (skip for bools, already-valid params, and certain tools)
        # For simpler tools like GetAlbUrl, MonitorDeployment, and TriggerBuild, hallucination check is too strict
        # These tools should infer from conversation context
        is_bool = _is_bool_field(annotation)
        skip_hallucination_check = tool_name in ["GetAlbUrl", "MonitorDeployment", "TriggerBuild"]

        if not is_bool and not skip_hallucination_check and pname not in _state["valid"]:
            # Check if value appears in message (with some flexibility)
            value_lower = str(value).lower()
            # Allow for variations like "python-app" vs "python app" vs "pythonapp"
            value_parts = value_lower.replace("-", " ").replace("_", " ").split()
            msg_sanitized = msg_lower.replace("-", " ").replace("_", " ")

            # Check if all parts of the value appear in the message
            value_found = all(part in msg_sanitized for part in value_parts) if value_parts else value_lower in msg_lower

            if not value_found:
                hallucinated[pname] = {"value": value, "reason": "value not found in user message"}
                continue

        errors = []

        # Boolean normalization
        _TRUE_WORDS = {"true", "yes", "y", "1", "enable", "enabled", "on", "active", "want", "need", "required", "with"}
        _FALSE_WORDS = {"false", "no", "n", "0", "disable", "disabled", "off", "inactive", "without", "none", "skip", "not"}

        if is_bool:
            if isinstance(value, str):
                val_lower = value.lower()
                if val_lower in _TRUE_WORDS:
                    value = True
                elif val_lower in _FALSE_WORDS:
                    value = False
                else:
                    all_words = list(_TRUE_WORDS) + list(_FALSE_WORDS)
                    matches = difflib.get_close_matches(val_lower, all_words, n=1, cutoff=0.7)
                    if matches:
                        matched = matches[0]
                        value = True if matched in _TRUE_WORDS else False
                        corrected[pname] = {"original": val_lower, "corrected_to": matched, "value": value}
                    else:
                        errors.append(f"'{value}' is not recognized. Use yes/no, true/false, enable/disable, or on/off")

        # Clean value
        if isinstance(value, str):
            value = value.strip(" ,;.'\"")

        # Enum check with fuzzy matching
        if not errors:
            enum_vals = _get_enum_values(annotation)
            if enum_vals and value not in enum_vals:
                enum_matches = difflib.get_close_matches(str(value).lower(), enum_vals, n=1, cutoff=0.6)
                if enum_matches:
                    corrected[pname] = {"original": value, "corrected_to": enum_matches[0]}
                    value = enum_matches[0]
                else:
                    errors.append(f"allowed options: {enum_vals}")

        if errors:
            invalid[pname] = {"value": new_params[pname], "reason": "; ".join(errors)}
            _state["valid"].pop(pname, None)
        else:
            valid_new[pname] = value

    # Merge new valid values
    _state["valid"].update(valid_new)

    # Build valid list in field order
    field_order = list(model.model_fields.keys())

    def _valid_list() -> list:
        result = []
        for k in field_order:
            if k in _state["valid"]:
                result.append({"param": k, "value": _state["valid"][k]})
            elif not model.model_fields[k].is_required() and model.model_fields[k].default is not None:
                result.append({"param": k, "value": model.model_fields[k].default, "default": True})
        return result

    # Find missing params
    missing = []
    for pname in field_order:
        field_info = model.model_fields[pname]
        if pname in _state["valid"]:
            continue
        if field_info.is_required():
            missing.append({"param": pname, "hint": _get_field_hint(pname, model), "required": True})
        # Note: Optional params with default=None are intentionally NOT added to missing list
        # They should not block execution

    # Auto-execute if all required params are valid (ignore optional params)
    required_missing = [m for m in missing if m.get("required", False)]
    if not invalid and not required_missing and not hallucinated:
        try:
            validated = model(**_state["valid"])
        except ValidationError as e:
            _state["valid"] = {k: v for k, v in _state["valid"].items() if k in model.model_fields}
            return {"valid": _valid_list(), "invalid": {}, "missing": [], "hallucinated": {}, "validation_error": str(e)}

        executor = EXECUTORS.get(tool_name)
        if executor:
            # Check if executor is async
            import inspect
            if inspect.iscoroutinefunction(executor):
                result = await executor(validated)
            else:
                result = executor(validated)
            _state = {"tool_name": None, "valid": {}}
            return {"result": result}

    response = {"valid": _valid_list(), "invalid": invalid, "missing": missing, "hallucinated": hallucinated}
    if corrected:
        response["corrected"] = corrected
    return response


def _build_tools_summary() -> str:
    """Build a human-readable summary of available tools."""
    parts = []
    for tool_name, model in TOOL_MODELS.items():
        lines = [f"{tool_name}: {model.__doc__ or ''}"]
        for pname, field_info in model.model_fields.items():
            desc = field_info.description or ""
            annotation = _get_field_annotation(pname, model)
            if annotation:
                enum_vals = _get_enum_values(annotation)
                if enum_vals:
                    desc += f" Options: {enum_vals}"
                if _is_bool_field(annotation):
                    desc += " (true/false)"
            req = "required" if field_info.is_required() else "optional"
            lines.append(f"  - {pname} ({req}): {desc}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)



# app/mcp_server.py

import json
from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP

from app.infra_chat_agent_with_tools.mcp_server.eks.eks_onboarding_server import validate_params_structured, _build_tools_summary


def create_mcp_server() -> FastMCP:
    """
    Creates DevLift MCP server instance.
    """

    mcp = FastMCP(
        name="DevLift EKS Service Manager",
        json_response=True,  # important for HTTP mode
        stateless_http=True
    )

    tools_summary = _build_tools_summary()

    async def host_eks_service(
        tool_name: Annotated[
            str,
            Field(description="Must be 'HostService'")
        ],
        params: Annotated[
            str,
            Field(description="JSON string with parameters")
        ],
        user_message: Annotated[
            str,
            Field(description="Original user message")
        ],
    ) -> str:

        try:
            params_dict = json.loads(params)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON: {e}"})

        result = await validate_params_structured(
            tool_name=tool_name,
            new_params=params_dict,
            user_message=user_message,
        )

        return json.dumps(result, default=str)

    async def get_alb_url(
        tool_name: Annotated[
            str,
            Field(description="Must be 'GetAlbUrl'")
        ],
        params: Annotated[
            str,
            Field(description="JSON string with parameters")
        ],
        user_message: Annotated[
            str,
            Field(description="Original user message")
        ],
    ) -> str:

        try:
            params_dict = json.loads(params)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON: {e}"})

        result = await validate_params_structured(
            tool_name=tool_name,
            new_params=params_dict,
            user_message=user_message,
        )

        return json.dumps(result, default=str)

    async def monitor_deployment_status(
        tool_name: Annotated[
            str,
            Field(description="Must be 'MonitorDeployment'")
        ],
        params: Annotated[
            str,
            Field(description="JSON string with parameters")
        ],
        user_message: Annotated[
            str,
            Field(description="Original user message")
        ],
    ) -> str:

        try:
            params_dict = json.loads(params)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON: {e}"})

        result = await validate_params_structured(
            tool_name=tool_name,
            new_params=params_dict,
            user_message=user_message,
        )

        return json.dumps(result, default=str)

    description = f"""
Generate EKS configuration files and DEPLOY infrastructure to Kubernetes cluster.

WORKFLOW:
1. Generate K8s manifests with Docker image reference (image doesn't exist yet)
2. Deploy to EKS immediately using kubectl (ALB created, pods show ImagePullBackOff - normal behavior)
3. Return ALB URL + files to commit (Dockerfile, GitHub Actions workflow)
4. User commits/pushes files → GitHub Actions builds image → Deployment completes

PREREQUISITES:
- kubectl must be installed and configured for target EKS cluster
- User must have RBAC permissions to create K8s resources
- Run: aws eks update-kubeconfig --name devlift-dev-cluster --region ap-south-1

DEPLOYMENT BEHAVIOR:
- Infrastructure (namespace, deployment, service, ingress, ALB) is created immediately
- Pods will be in ImagePullBackOff state initially
- ALB URL is available within seconds after deployment
- After committing files, GitHub Actions builds the Docker image and pushes to ECR
- Deployment automatically pulls the new image and application becomes live

ALLOWED CUSTOMIZATIONS (only if needed by the application):
- Dockerfile: Adjust EXPOSE port, ENTRYPOINT/CMD, WORKDIR, environment variables

IMPORTANT: Only provide parameters that the user explicitly mentions. All optional parameters have sensible defaults that should NOT be changed unless the user specifically requests it.

Required parameters (MUST be provided):
- service_name: Name of the service to host (extract from folder name or other sensible identifier or user's request)
- language: Programming language (java-gradle, java-maven, golang, go, python, nodejs, node) (detect from project or ask user)
- version: Language/runtime version (e.g., '17' for Java, '1.24' for Go, '3.11' for Python, '20' for Node.js) (detect from project or ask user)

NOTE: The tool validates parameters iteratively. If it returns 'missing' or 'invalid' parameters, extract those from the user's message or project context and call the tool again. Only when all required parameters are validated will it generate the files.

Optional parameters (ONLY provide if user explicitly requests - use defaults otherwise):
- port: Application port (default: auto-detected from language)
- environment: Deployment environment (default: stage)
- branch: Git branch for workflow trigger (default: main)
- generate_dockerfile: Whether to generate Dockerfile (default: true). Set to false if project already has a Dockerfile
- dockerfile_path: Path to existing Dockerfile (e.g., 'docker/Dockerfile', 'Dockerfile.prod'). Only needed if using a non-standard Dockerfile location

Full parameter reference:
{tools_summary}

CRITICAL: Do NOT override defaults for optional parameters unless the user explicitly requests changes to those specific settings.
"""

    mcp.add_tool(
        host_eks_service,
        name="host_eks_service",
        description=description,
    )

    alb_url_description = """
Get the public URL/endpoint for accessing a deployed service.

**PURPOSE**: Returns the ALB URL where the service can be accessed by users/clients.

**USE THIS TOOL ONLY WHEN USER ASKS FOR:**
- "What is the URL for [service]?"
- "How do I access [service]?"
- "What is the endpoint for [service]?"
- "Give me the public URL"

**DO NOT USE FOR DEPLOYMENT STATUS - This tool does NOT check:**
❌ If GitHub Actions deployment succeeded/failed
❌ If the application is running correctly
❌ Deployment workflow status
❌ General "deployment status" questions

For deployment status, use monitor_deployment_status instead.

**Parameters:**
- service_name: (REQUIRED) Name of the service
- namespace: (optional) Defaults to service_name

**Returns:**
- alb_url: Public URL (e.g., "http://abc123.elb.amazonaws.com")
- status: ready/pending/not_found/error

Full parameter reference:
{tools_summary}
"""

    mcp.add_tool(
        get_alb_url,
        name="get_alb_url",
        description=alb_url_description,
    )

    monitor_description = """
Check GitHub Actions deployment workflow status once and return immediately.

**PURPOSE**: Check the current status of a GitHub Actions deployment workflow.

**IMPORTANT BEHAVIOR:**
- This tool checks status ONCE and returns immediately
- It does NOT poll internally
- The LLM MUST call this tool repeatedly with delays until status is 'success', 'failure', or timeout
- Response includes 'next_action' field: 'wait_and_retry' (call again after wait_seconds) or 'stop' (final status reached)

**USE THIS TOOL WHEN USER ASKS:**
✅ "What is my deployment status?"
✅ "Is my deployment done?"
✅ "Check deployment status"
✅ "Did my deployment succeed?"
✅ "Is the application deployed?"
✅ "Monitor my deployment"
✅ "Track deployment progress"

**THIS IS THE RIGHT TOOL FOR DEPLOYMENT STATUS QUESTIONS.**

**REQUIRED INFORMATION:**
This tool needs these parameters. Infer from conversation context when possible:

1. **service_name** (REQUIRED) - The service being deployed
2. **github_repo** (REQUIRED) - GitHub repository in format: owner/repo (e.g., Regobs/my-app)
3. **branch** (defaults to "main") - Git branch
4. **environment** (defaults to "stage") - Deployment environment: dev/stage/qa/prod

**HOW IT WORKS:**
1. Finds the latest GitHub Actions workflow run for the service
2. Returns current status immediately
3. If response shows 'next_action': 'wait_and_retry', wait for 'wait_seconds' seconds and call again
4. Repeat until 'next_action' is 'stop' or max_duration is exceeded

**Parameters:**
- service_name: (REQUIRED) Service name
- github_repo: (REQUIRED) Repository in format "owner/repo"
- branch: (default: "main") Git branch
- environment: (default: "stage") Environment: dev/stage/qa/prod
- poll_interval: (default: 10) Recommended seconds to wait before next check
- max_duration: (default: 600) Maximum total time to keep checking in seconds

**Returns:**
- status: success/failure/in_progress/not_found/error
- run_url: GitHub Actions workflow URL
- run_id: Workflow run ID
- workflow_name: Name of the workflow
- conclusion: Workflow result (if completed)
- message: Detailed status message
- next_action: 'wait_and_retry' or 'stop'
- wait_seconds: Recommended wait time before next check (if next_action is 'wait_and_retry')

**POLLING LOGIC:**
The LLM MUST implement the polling loop:
1. Call this tool
2. Check 'next_action' in response
3. If 'wait_and_retry': wait for 'wait_seconds', then call again
4. If 'stop': polling complete, show final result
5. Track total elapsed time against max_duration

Full parameter reference:
{tools_summary}
"""

    mcp.add_tool(
        monitor_deployment_status,
        name="monitor_deployment_status",
        description=monitor_description,
    )

    async def trigger_build(
        tool_name: Annotated[
            str,
            Field(description="Must be 'TriggerBuild'")
        ],
        params: Annotated[
            str,
            Field(description="JSON string with parameters")
        ],
        user_message: Annotated[
            str,
            Field(description="Original user message")
        ],
    ) -> str:

        try:
            params_dict = json.loads(params)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid JSON: {e}"})

        result = await validate_params_structured(
            tool_name=tool_name,
            new_params=params_dict,
            user_message=user_message,
        )

        return json.dumps(result, default=str)

    trigger_description = """
Trigger a GitHub Actions workflow to rebuild and redeploy a service.

**PURPOSE**: Manually trigger a deployment workflow without committing new code.

**USE THIS TOOL WHEN USER ASKS:**
✅ "Trigger build"
✅ "Redeploy" / "Redeploy the application"
✅ "Rebuild"
✅ "Run deployment"
✅ "Trigger GitHub Actions"
✅ "Deploy again"

**CONTEXT AWARENESS:**
- If user just deployed a service, use that service's information
- Check recent conversation history for service name and repo
- Infer from most recently mentioned service in the conversation

**REQUIRED INFORMATION:**
1. **service_name** (REQUIRED) - The service to rebuild (infer from context)
2. **github_repo** (REQUIRED) - Repository in format: owner/repo (infer from recent deployment)
3. **branch** (defaults to "main") - Git branch
4. **environment** (defaults to "stage") - Environment: dev/stage/qa/prod

**HOW IT WORKS:**
1. Determines workflow filename from service name and environment
2. Calls GitHub API to trigger workflow_dispatch event
3. Returns success/failure with workflow URL

**Parameters:**
- service_name: (REQUIRED) Service name to redeploy
- github_repo: (REQUIRED) Repository in format "owner/repo"
- branch: (default: "main") Git branch
- environment: (default: "stage") Environment

**Returns:**
- status: success/error
- message: Descriptive message
- workflow_url: Link to triggered GitHub Actions run

Full parameter reference:
{tools_summary}
"""

    mcp.add_tool(
        trigger_build,
        name="trigger_build",
        description=trigger_description,
    )

    return mcp
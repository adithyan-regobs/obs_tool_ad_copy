"""
API endpoints for Dockerfile operations.
Provides endpoints to fetch Dockerfiles from repositories and templates.
"""
from typing import Tuple, Optional
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.service_config_service import ServiceConfigService
from app.services.dockerfile_fetch_service import DockerfileFetchService
from app.repository.language_ref_repository import LanguageRefRepository
from app.schemas.dockerfile_schemas import (
    DockerfileFromRepoResponse,
    DockerfileTemplateResponse,
    DockerfileGenerateRequest,
    DockerfileGenerateResponse,
    DockerfileModifyRequest,
    DockerfileModifyResponse
)
from app.utils.dockerfile_transformer import transform_dockerfile_for_datadog
from app.core.config import settings
from app.utils.github_app_token import get_token_for_org

router = APIRouter()


@router.get("/from-repo", response_model=DockerfileFromRepoResponse, summary="Fetch Dockerfile from Repository")
async def fetch_dockerfile_from_repo(
    repository: str = Query(..., description="Repository in format 'owner/repo' (e.g., 'myorg/myservice')"),
    branch: str = Query(..., description="Branch name (e.g., 'main', 'stage-env')"),
    dockerfile_path: Optional[str] = Query(None, description="Path to Dockerfile in repository (default: 'Dockerfile')"),
    service_config_code: Optional[str] = Query(None, description="Service config code to check for cached Dockerfile"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Fetch Dockerfile content from a GitHub repository.

    Security:
        - JWT authentication required
        - Uses GitHub App token for repository access

    This endpoint fetches the raw Dockerfile content from a specified repository and branch.
    If `service_config_code` is provided, it first checks the database for a cached Dockerfile.

    **Parameters:**
    - `repository`: Repository in format 'owner/repo' (e.g., 'Regobs/regobs-backend')
    - `branch`: Branch name (e.g., 'main', 'stage-env', 'develop')
    - `dockerfile_path`: Path to Dockerfile (default: 'Dockerfile')
      - Examples: 'Dockerfile', 'docker/myservice/Dockerfile', 'deploy/Dockerfile'
    - `service_config_code`: (Optional) Service config code to check for cached Dockerfile in DB

    **Response:**
    ```json
    {
        "content": "FROM openjdk:21-jdk-slim\\n...",
        "repository": "myorg/myservice",
        "branch": "main",
        "path": "Dockerfile",
        "sha": "abc123...",
        "url": "https://api.github.com/repos/myorg/myservice/contents/Dockerfile"
    }
    ```

    **Use Cases:**
    - Preview Dockerfile before modifying service config
    - Compare Dockerfiles across branches
    - Audit existing Dockerfiles in repositories
    - Get cached Dockerfile from service config (if service_config_code provided)
    """
    try:
        user, tenant = user_and_tenant

        # Default dockerfile_path to "Dockerfile" if not provided
        dockerfile_path = dockerfile_path or "Dockerfile"

        # Extract owner from repository (format: "owner/repo")
        owner = repository.split("/")[0] if "/" in repository else repository
        github_token = await get_token_for_org(owner, db)
        github_base_url = getattr(settings, 'github_api_url', 'https://api.github.com')

        # Call service layer to get Dockerfile from DB or fetch from GitHub
        service = ServiceConfigService(db)
        result = await service.get_dockerfile_from_db_or_fetch(
            service_config_code=service_config_code,
            tenant_code=tenant.code,
            repository=repository,
            branch=branch,
            dockerfile_path=dockerfile_path,
            github_token=github_token,
            github_base_url=github_base_url
        )

        return DockerfileFromRepoResponse(**result)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch Dockerfile: {str(e)}") 


@router.get("/template", response_model=DockerfileTemplateResponse, summary="Fetch Dockerfile Template")
async def fetch_dockerfile_template(
    language_ref_code: str = Query(..., description="Language reference code (e.g., 'java_21_lts_language_ref', 'go_1_23_language_ref')"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Fetch Dockerfile template from local templates directory.

    Security:
        - JWT authentication required

    This endpoint returns the standardized Dockerfile template for a specific language.
    Templates are stored locally in the `templates/dockerfiles/` directory.

    **Supported Languages:**
    - Java (any version) → `java-standard.Dockerfile`
    - Go (any version) → `go-standard.Dockerfile`

    **Parameters:**
    - `language_ref_code`: Language reference code from language_ref table
      - Examples: 'java_21_lts_language_ref', 'java_17_lts_language_ref', 'go_1_23_language_ref'

    **Response:**
    ```json
    {
        "content": "FROM openjdk:{{JDK_VERSION}}-jdk-slim\\n...",
        "language": "java",
        "version": "21",
        "template_name": "java-standard.Dockerfile",
        "template_path": "/path/to/templates/dockerfiles/java-standard.Dockerfile"
    }
    ```

    **Use Cases:**
    - Preview template before enabling generate_dockerfile
    - Understand what Dockerfile will be generated
    - Create custom Dockerfiles based on templates
    """
    try:
        user, tenant = user_and_tenant

        # Get language_ref from database
        language_repo = LanguageRefRepository(db)
        language_ref = await language_repo.get_by_code(language_ref_code)

        if not language_ref:
            raise HTTPException(
                status_code=404,
                detail=f"Language reference not found: {language_ref_code}"
            )

        # Fetch template
        service = DockerfileFetchService()
        result = service.fetch_template(language_ref)

        return DockerfileTemplateResponse(**result)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch template: {str(e)}")


@router.post("/generate", response_model=DockerfileGenerateResponse, summary="Generate Dockerfile with Placeholders Replaced")
async def generate_dockerfile(
    request: DockerfileGenerateRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Generate Dockerfile with all placeholders replaced (preview what will be pushed to Git).

    Security:
        - JWT authentication required

    This endpoint generates a **fresh Dockerfile** with all placeholders replaced,
    showing exactly what would be pushed to the repository when `generate_dockerfile` is enabled.
    Always generates a new Dockerfile (no database caching).

    **Supported Languages:**
    - **Java**: Generates with JDK version, optional Datadog APM integration
    - **Go**: Generates with port, optional config path and AWS Secrets Manager

    **Parameters (in request body):**

    **Required:**
    - `language_ref_code`: Language reference code (e.g., 'java_21_lts_language_ref')
    - `service_name`: Service name for Datadog/documentation

    **Java-specific (optional):**
    - `enable_datadog`: Enable Datadog APM (default: false)
    - `xms`: Java heap min in MB
    - `xmx`: Java heap max in MB
    - `advanced_options`: Datadog advanced configuration array

    **Go-specific (optional):**
    - `port`: Application port (default: "8080")
    - `go_config_path`: Config file path (e.g., "configs/config.yaml")
    - `use_aws_secrets`: Enable AWS Secrets Manager (default: false)

    **Example Request (Java with Datadog):**
    ```json
    {
        "language_ref_code": "java_21_lts_language_ref",
        "service_name": "my-service",
        "enable_datadog": true,
        "xms": 512,
        "xmx": 1024
    }
    ```

    **Example Request (Go with config):**
    ```json
    {
        "language_ref_code": "go_1_23_language_ref",
        "service_name": "my-go-service",
        "port": "8080",
        "go_config_path": "configs/config.yaml",
        "use_aws_secrets": true
    }
    ```

    **Response:**
    ```json
    {
        "content": "FROM eclipse-temurin:21-jdk\\n\\nARG JAR_FILE...",
        "language": "java",
        "version": "21",
        "service_name": "my-service",
        "parameters_used": {
            "service_name": "my-service",
            "language_version": "21",
            "enable_datadog": true,
            "xms": 512,
            "xmx": 1024
        }
    }
    ```

    **Use Cases:**
    - Preview generated Dockerfile before enabling `generate_dockerfile`
    - Test different configurations (with/without Datadog, different memory settings)
    - Understand what will be pushed to repository
    """
    try:
        user, tenant = user_and_tenant

        # Call service layer to generate fresh Dockerfile
        service = ServiceConfigService(db)
        result = await service.generate_dockerfile(
            tenant_code=tenant.code,
            language_ref_code=request.language_ref_code,
            service_name=request.service_name,
            enable_datadog=request.enable_datadog,
            xms=request.xms,
            xmx=request.xmx,
            advanced_options=request.advanced_options,
            port=request.port,
            go_config_path=request.go_config_path,
            use_aws_secrets=request.use_aws_secrets,
            build_args=request.build_args
        )

        return DockerfileGenerateResponse(**result)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate Dockerfile: {str(e)}")


@router.post("/modify", response_model=DockerfileModifyResponse, summary="Modify Existing Dockerfile")
async def modify_existing_dockerfile(
    request: DockerfileModifyRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Modify an existing Dockerfile from repository with build args and memory settings.

    Security:
        - JWT authentication required
        - Uses GitHub App token for repository access

    This endpoint fetches a Dockerfile from a repository and applies modifications:
    - Injects custom build arguments as ARG declarations
    - Updates JVM memory settings (-Xms/-Xmx) in JAVA_TOOL_OPTIONS
    - Optionally applies Datadog configuration

    **Parameters (in request body):**

    **Required:**
    - `repository`: Repository in format 'owner/repo'
    - `branch`: Branch name

    **Optional:**
    - `dockerfile_path`: Path to Dockerfile (default: 'Dockerfile')
    - `service_config_code`: Service config code to check for cached Dockerfile
    - `xms`: Java heap min in MB
    - `xmx`: Java heap max in MB
    - `enable_datadog`: Enable Datadog APM transformation
    - `service_name`: Service name for Datadog (required if enable_datadog=true)
    - `advanced_options`: Datadog advanced options
    - `build_args`: Custom Docker build arguments

    **Example Request:**
    ```json
    {
        "repository": "myorg/myservice",
        "branch": "main",
        "dockerfile_path": "Dockerfile",
        "xms": 512,
        "xmx": 1024,
        "build_args": [
            {"name": "APP_VERSION", "value": "1.0.0"},
            {"name": "BUILD_NUMBER", "value": "123"}
        ]
    }
    ```

    **Response:**
    ```json
    {
        "content": "FROM eclipse-temurin:21-jdk\\n...",
        "repository": "myorg/myservice",
        "branch": "main",
        "path": "Dockerfile",
        "sha": "abc123...",
        "modifications_applied": {
            "build_args_added": ["APP_VERSION", "BUILD_NUMBER"],
            "memory_updated": true,
            "datadog_enabled": false
        }
    }
    ```

    **Use Cases:**
    - Preview what modifications will be applied to an existing Dockerfile
    - Test build args and memory settings before saving
    - Preview Datadog integration on existing Dockerfiles
    """
    try:
        user, tenant = user_and_tenant

        # Default dockerfile_path to "Dockerfile" if not provided
        dockerfile_path = request.dockerfile_path or "Dockerfile"

        # Extract owner from repository (format: "owner/repo")
        owner = request.repository.split("/")[0] if "/" in request.repository else request.repository
        github_token = await get_token_for_org(owner, db)
        github_base_url = getattr(settings, 'github_api_url', 'https://api.github.com')

        # Call service layer to get Dockerfile from DB or fetch from GitHub
        service = ServiceConfigService(db)
        result = await service.get_dockerfile_from_db_or_fetch(
            service_config_code=request.service_config_code,
            tenant_code=tenant.code,
            repository=request.repository,
            branch=request.branch,
            dockerfile_path=dockerfile_path,
            github_token=github_token,
            github_base_url=github_base_url
        )

        original_content = result.get("content", "")
        modified_content = original_content
        modifications_applied = {
            "build_args_added": [],
            "memory_updated": False,
            "datadog_enabled": False
        }

        # Initialize DockerfileFetchService for modifications
        dockerfile_service = DockerfileFetchService()

        # Apply Datadog transformation if requested
        if request.enable_datadog and request.service_name:
            modified_content = transform_dockerfile_for_datadog(
                dockerfile_content=modified_content,
                service_name=request.service_name,
                xms_mb=request.xms,
                xmx_mb=request.xmx,
                advanced_options=request.advanced_options
            )
            modifications_applied["datadog_enabled"] = True
            modifications_applied["memory_updated"] = bool(request.xms or request.xmx)
        else:
            # Apply memory settings separately (if not using Datadog which already includes them)
            if request.xms or request.xmx:
                modified_content = dockerfile_service.update_java_memory_settings(
                    modified_content,
                    xms_mb=request.xms,
                    xmx_mb=request.xmx
                )
                modifications_applied["memory_updated"] = True

        # Inject build args
        if request.build_args:
            modified_content = dockerfile_service.inject_build_args(
                modified_content,
                request.build_args
            )
            # Track which args were actually added (not duplicates)
            added_args = [arg.get("name", arg.get("key", "")).upper()
                          for arg in request.build_args if arg.get("name") or arg.get("key")]
            modifications_applied["build_args_added"] = added_args

        return DockerfileModifyResponse(
            content=modified_content,
            repository=request.repository,
            branch=request.branch,
            path=dockerfile_path,
            sha=result.get("sha"),
            modifications_applied=modifications_applied
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to modify Dockerfile: {str(e)}")

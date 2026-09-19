"""
Pipeline Management Endpoints

This module provides API endpoints for managing CI/CD pipelines.
Currently uses in-memory dummy data for demonstration purposes.

Endpoints:
    POST /pipelines/get-all-pipelines - Get all pipelines with filtering and pagination
    POST /pipelines/create-pipeline - Create a new pipeline
    GET /pipelines/languages - Get list of supported programming languages
    GET /pipelines/language-versions/{language} - Get versions for a specific language
    GET /pipelines/repositories - Get list of available repositories
    GET /pipelines/branches - Get list of available branches
"""

from typing import List, Dict, Tuple
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.schemas.pipeline_schemas import (
    CreatePipelineRequest,
    CreatePipelineResponse,
    GetAllPipelinesRequest,
    GetAllPipelinesResponse,
    LanguagesResponse,
    LanguageVersionsResponse,
    RepositoriesResponse,
    BranchesResponse
)

router = APIRouter()

# Supported languages and their versions
SUPPORTED_LANGUAGES = ["Java", "Golang", "Python"]

LANGUAGE_VERSIONS: Dict[str, List[str]] = {
    "Java": ["8", "11", "17", "21"],
    "Golang": ["1.19", "1.20", "1.21", "1.22"],
    "Python": ["3.8", "3.9", "3.10", "3.11", "3.12"],
}

# Available repositories
GITHUB_REPOSITORIES = [
    "company/auth-service",
    "company/payment-service",
    "company/user-service",
    "company/notification-service",
    "company/api-gateway",
]

# Available branches
GIT_BRANCHES = ["main", "develop", "staging", "release", "hotfix"]

# In-memory storage for created pipelines (persists during server runtime)
created_pipelines: List[Dict] = []

# Counter for auto-incrementing pipeline IDs
pipeline_id_counter = 1


def generate_dummy_pipelines() -> List[Dict]:
    """
    Generate dummy pipeline data for demonstration.

    Returns:
        List of pipeline dictionaries with realistic data
    """
    dummy_data = [
        {
            "id": 1,
            "name": "payment-service-pipeline",
            "environment": "production",
            "last_deployment": (datetime.now() - timedelta(hours=2)).isoformat(),
            "deployment_status": "success",
            "build_logs": "https://jenkins.example.com/job/payment-service/123/console",
            "repo": "https://github.com/acme-corp/payment-service",
            "branch": "main",
            "language": "Java",
            "version": "17",
            "deployment_number": 245
        },
        {
            "id": 2,
            "name": "user-auth-api",
            "environment": "production",
            "last_deployment": (datetime.now() - timedelta(hours=5)).isoformat(),
            "deployment_status": "success",
            "build_logs": "https://jenkins.example.com/job/user-auth-api/89/console",
            "repo": "https://github.com/acme-corp/user-auth-api",
            "branch": "main",
            "language": "Golang",
            "version": "1.22",
            "deployment_number": 156
        },
        {
            "id": 3,
            "name": "notification-service",
            "environment": "staging",
            "last_deployment": (datetime.now() - timedelta(minutes=30)).isoformat(),
            "deployment_status": "in_progress",
            "build_logs": "https://jenkins.example.com/job/notification-service/34/console",
            "repo": "https://github.com/acme-corp/notification-service",
            "branch": "develop",
            "language": "Python",
            "version": "3.11",
            "deployment_number": 67
        },
        {
            "id": 4,
            "name": "analytics-engine",
            "environment": "production",
            "last_deployment": (datetime.now() - timedelta(hours=12)).isoformat(),
            "deployment_status": "success",
            "build_logs": "https://jenkins.example.com/job/analytics-engine/201/console",
            "repo": "https://github.com/acme-corp/analytics-engine",
            "branch": "main",
            "language": "Python",
            "version": "3.12",
            "deployment_number": 312
        },
        {
            "id": 5,
            "name": "inventory-mgmt",
            "environment": "dev",
            "last_deployment": (datetime.now() - timedelta(minutes=15)).isoformat(),
            "deployment_status": "failed",
            "build_logs": "https://jenkins.example.com/job/inventory-mgmt/45/console",
            "repo": "https://github.com/acme-corp/inventory-mgmt",
            "branch": "feature/new-search",
            "language": "Java",
            "version": "21",
            "deployment_number": 23
        },
        {
            "id": 6,
            "name": "order-processing",
            "environment": "staging",
            "last_deployment": (datetime.now() - timedelta(hours=1)).isoformat(),
            "deployment_status": "success",
            "build_logs": "https://jenkins.example.com/job/order-processing/178/console",
            "repo": "https://github.com/acme-corp/order-processing",
            "branch": "release/v2.1",
            "language": "Golang",
            "version": "1.21",
            "deployment_number": 189
        },
        {
            "id": 7,
            "name": "customer-portal",
            "environment": "production",
            "last_deployment": (datetime.now() - timedelta(days=1)).isoformat(),
            "deployment_status": "success",
            "build_logs": "https://jenkins.example.com/job/customer-portal/567/console",
            "repo": "https://github.com/acme-corp/customer-portal",
            "branch": "main",
            "language": "Java",
            "version": "17",
            "deployment_number": 589
        },
        {
            "id": 8,
            "name": "api-gateway",
            "environment": "production",
            "last_deployment": (datetime.now() - timedelta(hours=8)).isoformat(),
            "deployment_status": "success",
            "build_logs": "https://jenkins.example.com/job/api-gateway/423/console",
            "repo": "https://github.com/acme-corp/api-gateway",
            "branch": "main",
            "language": "Golang",
            "version": "1.22",
            "deployment_number": 445
        },
        {
            "id": 9,
            "name": "data-migration-tool",
            "environment": "dev",
            "last_deployment": (datetime.now() - timedelta(minutes=45)).isoformat(),
            "deployment_status": "pending",
            "build_logs": "https://jenkins.example.com/job/data-migration-tool/12/console",
            "repo": "https://github.com/acme-corp/data-migration-tool",
            "branch": "feature/bulk-import",
            "language": "Python",
            "version": "3.10",
            "deployment_number": 8
        },
        {
            "id": 10,
            "name": "reporting-service",
            "environment": "staging",
            "last_deployment": (datetime.now() - timedelta(hours=3)).isoformat(),
            "deployment_status": "success",
            "build_logs": "https://jenkins.example.com/job/reporting-service/92/console",
            "repo": "https://github.com/acme-corp/reporting-service",
            "branch": "develop",
            "language": "Java",
            "version": "11",
            "deployment_number": 98
        },
        {
            "id": 11,
            "name": "cache-service",
            "environment": "production",
            "last_deployment": (datetime.now() - timedelta(hours=6)).isoformat(),
            "deployment_status": "success",
            "build_logs": "https://jenkins.example.com/job/cache-service/234/console",
            "repo": "https://github.com/acme-corp/cache-service",
            "branch": "main",
            "language": "Golang",
            "version": "1.20",
            "deployment_number": 267
        },
        {
            "id": 12,
            "name": "ml-model-service",
            "environment": "dev",
            "last_deployment": (datetime.now() - timedelta(hours=4)).isoformat(),
            "deployment_status": "failed",
            "build_logs": "https://jenkins.example.com/job/ml-model-service/15/console",
            "repo": "https://github.com/acme-corp/ml-model-service",
            "branch": "experiment/new-algorithm",
            "language": "Python",
            "version": "3.9",
            "deployment_number": 18
        },
    ]

    return dummy_data


@router.post("/get-all-pipelines", response_model=GetAllPipelinesResponse)
async def get_all_pipelines(
    data: GetAllPipelinesRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all pipelines with filtering and pagination.

    Security:
        - JWT authentication required

    This endpoint returns a list of all pipelines with support for:
    - Pagination (skip/limit)
    - Filtering by environment, language, and deployment status

    Args:
        data: GetAllPipelinesRequest containing filters and pagination parameters

    Returns:
        GetAllPipelinesResponse with total count and list of pipelines

    Example Request:
        POST /api/v1/pipelines/get-all-pipelines
        {
            "skip": 0,
            "limit": 10,
            "environment": "production",
            "language": "Java"
        }

    Example Response:
        {
            "total": 15,
            "skip": 0,
            "limit": 10,
            "pipelines": [
                {
                    "id": 1,
                    "name": "payment-service-pipeline",
                    "environment": "production",
                    "last_deployment": "2025-11-02T10:30:00",
                    "deployment_status": "success",
                    "build_logs": "https://jenkins.example.com/job/payment-service/123/console",
                    "repo": "https://github.com/acme-corp/payment-service",
                    "branch": "main",
                    "language": "Java",
                    "version": "17",
                    "deployment_number": 245
                }
            ]
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET ALL PIPELINES - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print(f"   Environment Filter: {data.environment.value if data.environment else 'All'}")
        print(f"   Language Filter: {data.language if data.language else 'All'}")
        print(f"   Status Filter: {data.deployment_status.value if data.deployment_status else 'All'}")
        print("="*80 + "\n")

        # Combine dummy data with created pipelines
        all_pipelines = generate_dummy_pipelines() + created_pipelines

        # Apply filters
        filtered_pipelines = all_pipelines

        if data.environment:
            filtered_pipelines = [
                p for p in filtered_pipelines
                if p["environment"] == data.environment.value
            ]

        if data.language:
            filtered_pipelines = [
                p for p in filtered_pipelines
                if p["language"] == data.language
            ]

        if data.deployment_status:
            filtered_pipelines = [
                p for p in filtered_pipelines
                if p["deployment_status"] == data.deployment_status.value
            ]

        # Get total count before pagination
        total = len(filtered_pipelines)

        # Apply pagination
        paginated_pipelines = filtered_pipelines[data.skip:data.skip + data.limit]

        return GetAllPipelinesResponse(
            total=total,
            skip=data.skip,
            limit=data.limit,
            pipelines=paginated_pipelines
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve pipelines: {str(e)}")


@router.post("/create-pipeline", response_model=CreatePipelineResponse)
async def create_pipeline(
    data: CreatePipelineRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Create a new pipeline.

    Security:
        - JWT authentication required

    This endpoint creates a new pipeline configuration and stores it in memory.
    Auto-generates fields like ID, deployment status, build logs URL, etc.

    Args:
        data: CreatePipelineRequest containing pipeline configuration

    Returns:
        CreatePipelineResponse with success status and created pipeline data

    Example Request:
        POST /api/v1/pipelines/create-pipeline
        {
            "name": "new-microservice",
            "repo": "https://github.com/acme-corp/new-microservice",
            "branch": "main",
            "language": "Java",
            "version": "17",
            "environment": "development"
        }

    Example Response:
        {
            "status": "success",
            "message": "Pipeline created successfully",
            "operation": "create_pipeline",
            "data": {
                "id": 13,
                "name": "new-microservice",
                "environment": "development",
                "deployment_status": "pending",
                ...
            }
        }
    """
    global pipeline_id_counter

    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 CREATE PIPELINE - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print(f"   Pipeline Name: {data.name}")
        print(f"   Language: {data.language} v{data.version}")
        print(f"   Environment: {data.environment.value}")
        print("="*80 + "\n")

        # Validate language version
        if data.language in LANGUAGE_VERSIONS:
            if data.version not in LANGUAGE_VERSIONS[data.language]:
                valid_versions = ", ".join(LANGUAGE_VERSIONS[data.language])
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid version for {data.language}. Valid versions: {valid_versions}"
                )

        # Generate new pipeline ID
        new_id = len(generate_dummy_pipelines()) + len(created_pipelines) + 1
        pipeline_id_counter = new_id

        # Create pipeline object with auto-generated fields
        new_pipeline = {
            "id": new_id,
            "name": data.name,
            "environment": data.environment.value,
            "last_deployment": datetime.now().isoformat(),
            "deployment_status": "pending",  # New pipelines start as pending
            "build_logs": f"https://jenkins.example.com/job/{data.name.replace(' ', '-')}/1/console",
            "repo": data.repo,
            "branch": data.branch,
            "language": data.language,
            "version": data.version,
            "deployment_number": 0  # New pipeline, no deployments yet
        }

        # Store in memory
        created_pipelines.append(new_pipeline)

        return CreatePipelineResponse(
            status="success",
            message=f"Pipeline '{data.name}' created successfully",
            operation="create_pipeline",
            data=new_pipeline
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create pipeline: {str(e)}")


@router.get("/languages", response_model=LanguagesResponse)
async def get_languages(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get list of supported programming languages.

    Security:
        - JWT authentication required

    Returns a list of all programming languages supported by the pipeline system.

    Returns:
        LanguagesResponse with list of supported languages

    Example Request:
        GET /api/v1/pipelines/languages

    Example Response:
        {
            "languages": ["Java", "Golang", "Python"]
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET LANGUAGES - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print("="*80 + "\n")

        return LanguagesResponse(languages=SUPPORTED_LANGUAGES)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve languages: {str(e)}")


@router.get("/language-versions/{language}", response_model=LanguageVersionsResponse)
async def get_language_versions(
    language: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get supported versions for a specific programming language.

    Security:
        - JWT authentication required

    Args:
        language: Programming language name (Java, Golang, or Python)

    Returns:
        LanguageVersionsResponse with language and its supported versions

    Example Request:
        GET /api/v1/pipelines/language-versions/Java

    Example Response:
        {
            "language": "Java",
            "versions": ["8", "11", "17", "21"]
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET LANGUAGE VERSIONS - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print(f"   Language: {language}")
        print("="*80 + "\n")

        # Validate language
        if language not in LANGUAGE_VERSIONS:
            raise HTTPException(
                status_code=404,
                detail=f"Language '{language}' not found. Supported languages: {', '.join(SUPPORTED_LANGUAGES)}"
            )

        return LanguageVersionsResponse(
            language=language,
            versions=LANGUAGE_VERSIONS[language]
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve language versions: {str(e)}")


@router.get("/repositories", response_model=RepositoriesResponse)
async def get_repositories(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get list of available repositories.

    Security:
        - JWT authentication required

    Returns a list of all GitHub repositories available for pipeline creation.

    Returns:
        RepositoriesResponse with list of repositories

    Example Request:
        GET /api/v1/pipelines/repositories

    Example Response:
        {
            "repositories": [
                "company/auth-service",
                "company/payment-service",
                "company/user-service",
                "company/notification-service",
                "company/api-gateway"
            ]
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET REPOSITORIES - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print("="*80 + "\n")

        return RepositoriesResponse(repositories=GITHUB_REPOSITORIES)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve repositories: {str(e)}")


@router.get("/branches", response_model=BranchesResponse)
async def get_branches(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get list of available Git branches.

    Security:
        - JWT authentication required

    Returns a list of all Git branches available for pipeline configuration.

    Returns:
        BranchesResponse with list of branches

    Example Request:
        GET /api/v1/pipelines/branches

    Example Response:
        {
            "branches": ["main", "develop", "staging", "release", "hotfix"]
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET BRANCHES - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print("="*80 + "\n")

        return BranchesResponse(branches=GIT_BRANCHES)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve branches: {str(e)}")

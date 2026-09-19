"""
Language Reference API Endpoints

This module provides REST API endpoints for managing and retrieving language references.
"""

from typing import Optional, Tuple
from fastapi import APIRouter, HTTPException, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.language_ref_service import LanguageRefService
from app.schemas.language_ref_schemas import (
    LanguageVersionsResponse,
    LanguageVersionsGroupedResponse,
    LanguageRefResponse
)
router = APIRouter()


@router.get(
    "/language-versions",
    response_model=LanguageVersionsResponse,
    summary="Get All Language Versions",
    description="""
    Retrieve all active language versions with optional filtering by CI/CD platform.

    This endpoint returns a flat list of all available language versions that can be used
    for creating pipelines. Each entry includes the language name, version, code, and
    yaml_templates for different platforms.

    **Use Cases:**
    - Populate dropdown menus in pipeline creation forms
    - Display all available language/version combinations
    - Filter languages by specific CI/CD platform

    **Example Response:**
    ```json
    {
        "total": 30,
        "languages": [
            {
                "id": 1,
                "code": "PYTHON_3_12",
                "name": "Python 3.12",
                "version": "3.12",
                "yaml_templates": {
                    "github_actions": "/templates/github-actions/python-3.12.yml",
                    "gitlab_ci": "/templates/gitlab-ci/python-3.12.yml"
                },
                "created_at": "2025-01-04T12:00:00Z",
                "is_active": true
            }
        ]
    }
    ```
    """
)
async def get_all_language_versions(
    platform: Optional[str] = Query(
        None,
        description="Filter by CI/CD platform (e.g., github_actions, gitlab_ci)"
    ),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all language versions, optionally filtered by platform.

    Security:
        - JWT authentication required
        - Available to all authenticated users (reference data)

    Args:
        platform: Optional platform filter (checks if platform key exists in yaml_templates)
        db: Database session

    Returns:
        LanguageVersionsResponse with list of language versions
    """
    try:
        user, tenant = user_and_tenant
        print(f"🔍 GET LANGUAGE VERSIONS - User: {user.code}, Tenant: {tenant.code}, Platform: {platform}")

        service = LanguageRefService(db)
        result = await service.get_all_language_versions(platform=platform)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/language-versions/grouped",
    response_model=LanguageVersionsGroupedResponse,
    summary="Get Language Versions Grouped by Language",
    description="""
    Retrieve language versions grouped by base language name.

    This endpoint organizes language versions by their base language (e.g., all Python versions
    together, all Node.js versions together). This is useful for hierarchical UI components
    like nested dropdowns or expandable lists.

    **Use Cases:**
    - Display languages in a tree structure
    - Show version options after language selection
    - Group similar technologies together

    **Example Response:**
    ```json
    {
        "total_languages": 12,
        "total_versions": 30,
        "languages": [
            {
                "language_name": "Python",
                "versions": [
                    {
                        "id": 1,
                        "code": "PYTHON_3_12",
                        "name": "Python 3.12",
                        "version": "3.12",
                        "yaml_templates": {
                            "github_actions": "/templates/github-actions/python-3.12.yml"
                        }
                    },
                    {
                        "id": 2,
                        "code": "PYTHON_3_11",
                        "name": "Python 3.11",
                        "version": "3.11",
                        "yaml_templates": {
                            "github_actions": "/templates/github-actions/python-3.11.yml"
                        }
                    }
                ]
            }
        ]
    }
    ```
    """
)
async def get_language_versions_grouped(
    platform: Optional[str] = Query(
        None,
        description="Filter by CI/CD platform (e.g., github_actions, gitlab_ci)"
    ),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get language versions grouped by base language name.

    Security:
        - JWT authentication required
        - Available to all authenticated users (reference data)

    Args:
        platform: Optional platform filter (checks if platform key exists in yaml_templates)
        db: Database session

    Returns:
        LanguageVersionsGroupedResponse with grouped languages
    """
    try:
        user, tenant = user_and_tenant
        print(f"🔍 GET LANGUAGE VERSIONS GROUPED - User: {user.code}, Tenant: {tenant.code}")

        service = LanguageRefService(db)
        result = await service.get_language_versions_grouped(platform=platform)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/language-versions/{code}",
    response_model=LanguageRefResponse,
    summary="Get Language Version by Code",
    description="""
    Retrieve a specific language version by its unique code.

    **Example:**
    - `GET /language-versions/PYTHON_3_12_GITHUB` returns Python 3.12 for GitHub Actions
    """
)
async def get_language_version_by_code(
    code: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get a specific language version by code.

    Security:
        - JWT authentication required
        - Available to all authenticated users (reference data)

    Args:
        code: Language reference code (e.g., 'PYTHON_3_12_GITHUB')
        db: Database session

    Returns:
        LanguageRefResponse

    Raises:
        404: Language version not found
    """
    try:
        user, tenant = user_and_tenant
        print(f"🔍 GET LANGUAGE BY CODE - User: {user.code}, Code: {code}")

        service = LanguageRefService(db)
        result = await service.get_language_by_code(code)

        if not result:
            raise HTTPException(
                status_code=404,
                detail=f"Language version with code '{code}' not found"
            )

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/platforms/{platform}/languages",
    response_model=LanguageVersionsResponse,
    summary="Get Languages for Specific CI/CD Platform",
    description="""
    Retrieve all language versions that support a specific CI/CD platform.

    This endpoint filters languages by checking if the platform key exists in their
    yaml_templates JSONB field.

    **Use Cases:**
    - Show only GitHub Actions compatible languages
    - Filter by user's selected CI/CD platform
    - Display platform-specific options

    **Example:**
    - `GET /platforms/github_actions/languages` returns only languages with GitHub Actions templates
    - `GET /platforms/gitlab_ci/languages` returns only languages with GitLab CI templates
    """
)
async def get_languages_for_platform(
    platform: str,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all language versions that support a specific CI/CD platform.

    Security:
        - JWT authentication required
        - Available to all authenticated users (reference data)

    Args:
        platform: Platform name (e.g., github_actions, gitlab_ci, aws_codepipeline)
        db: Database session

    Returns:
        LanguageVersionsResponse with filtered languages
    """
    try:
        user, tenant = user_and_tenant
        print(f"🔍 GET LANGUAGES FOR PLATFORM - User: {user.code}, Tenant: {tenant.code}, Platform: {platform}")

        service = LanguageRefService(db)
        result = await service.get_languages_for_platform(platform)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

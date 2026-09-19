"""
Environment Variable Management Endpoints

This module provides API endpoints for managing environment variables and secrets.
Currently uses in-memory dummy data for demonstration purposes.

Endpoints:
    POST /environment-variables/get-all-variables - Get all environment variables with filtering and pagination
"""

from typing import List, Dict, Tuple
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.schemas.environment_variable_schemas import (
    GetAllVariablesRequest,
    GetAllVariablesResponse,
    VariableListItem
)

router = APIRouter()

# In-memory storage for environment variables (persists during server runtime)
created_variables: List[Dict] = []


def generate_dummy_variables() -> List[Dict]:
    """
    Generate dummy environment variable data for demonstration.

    Returns:
        List of environment variable dictionaries with realistic data
    """
    dummy_data = [
        {
            "id": "1",
            "key": "DATABASE_URL",
            "value": "postgresql://user:pass@host:5432/db",
            "type": "secret",
            "last_updated": "2024-10-28"
        },
        {
            "id": "2",
            "key": "API_TIMEOUT",
            "value": "30000",
            "type": "variable",
            "last_updated": "2024-10-27"
        },
        {
            "id": "3",
            "key": "MAX_CONNECTIONS",
            "value": "100",
            "type": "variable",
            "last_updated": "2024-10-26"
        },
        {
            "id": "4",
            "key": "AWS_SECRET_KEY",
            "value": "Sfjks23kjdfsljl",
            "type": "secret",
            "last_updated": "2024-10-25"
        },
        {
            "id": "5",
            "key": "REDIS_HOST",
            "value": "redis://localhost:6379",
            "type": "secret",
            "last_updated": "2024-10-24"
        },
        {
            "id": "6",
            "key": "LOG_LEVEL",
            "value": "INFO",
            "type": "variable",
            "last_updated": "2024-10-23"
        },
        {
            "id": "7",
            "key": "CACHE_TTL",
            "value": "3600",
            "type": "variable",
            "last_updated": "2024-10-22"
        },
        {
            "id": "8",
            "key": "JWT_SECRET",
            "value": "kfsjdklj343uhkj3dkj",
            "type": "secret",
            "last_updated": "2024-10-21"
        },
        {
            "id": "9",
            "key": "ENABLE_METRICS",
            "value": "true",
            "type": "variable",
            "last_updated": "2024-10-20"
        },
        {
            "id": "10",
            "key": "SMTP_PASSWORD",
            "value": "jfkdsh34jhkjhfdf",
            "type": "secret",
            "last_updated": "2024-10-19"
        },
        {
            "id": "11",
            "key": "MAX_RETRY_ATTEMPTS",
            "value": "3",
            "type": "variable",
            "last_updated": "2024-10-18"
        },
        {
            "id": "12",
            "key": "API_KEY",
            "value": "jfkdhkjh45j43h5",
            "type": "secret",
            "last_updated": "2024-10-17"
        },
    ]

    return dummy_data


@router.post("/get-all-variables", response_model=GetAllVariablesResponse)
async def get_all_variables(
    data: GetAllVariablesRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Get all environment variables with filtering and pagination.

    Security:
        - JWT authentication required
        - Available to all authenticated users

    This endpoint returns a list of all environment variables with support for:
    - Pagination (skip/limit)
    - Filtering by type (secret or variable)

    Args:
        data: GetAllVariablesRequest containing filters and pagination parameters

    Returns:
        GetAllVariablesResponse with total count and list of variables

    Example Request:
        POST /api/v1/environment-variables/get-all-variables
        {
            "skip": 0,
            "limit": 10,
            "type": "secret"
        }

    Example Response:
        {
            "total": 5,
            "skip": 0,
            "limit": 10,
            "variables": [
                {
                    "id": "1",
                    "key": "DATABASE_URL",
                    "value": "postgresql://user:pass@host:5432/db",
                    "type": "secret",
                    "lastUpdated": "2024-10-28"
                },
                {
                    "id": "4",
                    "key": "AWS_SECRET_KEY",
                    "value": "jfksdf2343jhfjfkdshkj",
                    "type": "secret",
                    "lastUpdated": "2024-10-25"
                }
            ]
        }
    """
    try:
        # Extract authenticated user and tenant from JWT
        user, tenant = user_and_tenant

        # DEBUG: Log authenticated access
        print("\n" + "="*80)
        print("🔍 GET ALL ENVIRONMENT VARIABLES - AUTHENTICATED ACCESS")
        print(f"   User Code: {user.code}")
        print(f"   Tenant Code: {tenant.code}")
        print(f"   Type Filter: {data.type.value if data.type else 'All'}")
        print("="*80 + "\n")
        # Combine dummy data with created variables
        all_variables = generate_dummy_variables() + created_variables

        # Apply type filter if provided
        filtered_variables = all_variables

        if data.type:
            filtered_variables = [
                v for v in filtered_variables
                if v["type"] == data.type.value
            ]

        # Get total count before pagination
        total = len(filtered_variables)

        # Apply pagination
        paginated_variables = filtered_variables[data.skip:data.skip + data.limit]

        return GetAllVariablesResponse(
            total=total,
            skip=data.skip,
            limit=data.limit,
            variables=paginated_variables
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve environment variables: {str(e)}")

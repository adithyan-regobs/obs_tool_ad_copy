from typing import List, Optional, Tuple
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_db, get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.schemas.case_schemas import CaseTypeRefItem, CaseRefItem, SearchCaseRefsRequest, CaseRefWithTypeItem
from app.services.case_reference_service import CaseReferenceService

router = APIRouter()

@router.get(
    "/case-types",
    response_model=List[CaseTypeRefItem],
    summary="Get All Case Types"
)
async def get_case_types(
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve all available case types.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Description:
        Returns a list of all case types configured in the system. 
        No filters or pagination supported for this endpoint.

    Response:
        - id: Case type unique identifier
        - code: Case type reference code
        - name: Case type display name
        - description: Optional description
        - vendor_provider: External vendor name if applicable
        - created_at / updated_at: Timestamps
        - is_active: Active/inactive status
        - is_deleted: Soft delete flag

    Example:
        GET /api/v1/case-types

    Returns:
        List[CaseTypeRefItem]
    """
    service = CaseReferenceService(db)
    result = await service.get_all_case_types()
    return result


@router.get(
    "/case-refs",
    response_model=List[CaseRefItem],
    summary="Get Case References"
)
async def get_case_refs(
    case_type_ref_code: Optional[str] = Query(None, description="Filter by case type reference code"),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Retrieve case reference items optionally filtered by case_type_ref_code.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Query Parameters:
        - case_type_ref_code (optional): Filter results by case type reference code

    Description:
        Useful to retrieve:
            - all case references, or
            - only references that belong to a specific case type.

    Response fields:
        - id: Case reference ID
        - code: Case reference code
        - name: Case reference name
        - description: Optional description
        - case_type_ref_code: Relationship to case type
        - created_at / updated_at: Timestamps
        - is_active / is_deleted: Status flags

    Example Requests:
        # Get all
        GET /api/v1/case-refs

        # Filter by type
        GET /api/v1/case-refs?case_type_ref_code=KYC_TYPE

    Response:
        List[CaseRefItem]
    """
    service = CaseReferenceService(db)
    result = await service.get_case_refs(case_type_ref_code=case_type_ref_code)
    return result


@router.post(
    "/search-case-refs",
    response_model=List[CaseRefWithTypeItem],
    summary="Search Case References"
)
async def search_case_refs(
    data: SearchCaseRefsRequest,
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
    db: AsyncSession = Depends(get_db)
):
    """
    Search case references by space-separated search terms.

    Security:
        - JWT authentication required
        - Tenant isolation enforced

    Request Body:
        - search_query: Space-separated search terms (e.g., "mysql user management")

    Description:
        Splits the search query by spaces and searches for matches in:
        - case_ref.name (ILIKE)
        - case_type_ref.name (ILIKE)

        Returns all case_ref entries where ANY term matches either field.

    Response fields:
        - id: Case reference ID
        - code: Case reference code
        - name: Case reference name
        - case_type_ref_code: Relationship to case type
        - case_type_name: Name from joined case_type_ref

    Example Request:
        POST /api/v1/search-case-refs
        {"search_query": "mysql user"}

    Returns:
        List[CaseRefWithTypeItem]
    """
    service = CaseReferenceService(db)
    return await service.search_case_refs(data.search_query)

"""API endpoints that proxy requests to the external new-chat backend."""

import logging
from typing import Tuple

from fastapi import APIRouter, Depends, Header, HTTPException
from typing import Optional
from pydantic import BaseModel
import httpx

from app.api.dependencies import get_current_user_and_tenant
from app.db.models.user_mst_model import UserMstModel
from app.db.models.tenants_mst_model import TenantsMstModel
from app.services.new_chat_service import NewChatService

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Request models (matching the external backend's payload signature) ---

class SelectFormRequest(BaseModel):
    form_id: str


class ChatRequest(BaseModel):
    message: str
    ticket_code: str


class SelectSessionRequest(BaseModel):
    ticket_code: str


# --- Endpoints ---

@router.post("/services")
async def get_services(
    authorization: Optional[str] = Header(None),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Proxy: list available services from the new-chat backend."""
    try:
        user, tenant = user_and_tenant

        # Extract raw JWT from the "Authorization: Bearer <token>" header
        jwt_token = None
        if authorization and authorization.lower().startswith("bearer "):
            jwt_token = authorization.split(" ", 1)[1].strip()

        payload = {
            "user_mst_code": user.code,
            "tenant_code": tenant.code,
            "jwt_token": jwt_token,
        }
        service = NewChatService()
        return await service.get_services(payload)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except httpx.RequestError as exc:
        logger.error("New-chat backend unreachable: %s", exc)
        raise HTTPException(status_code=502, detail="New-chat backend is unreachable")


@router.post("/select-service")
async def select_service(req: SelectFormRequest):
    """Proxy: select a service/form on the new-chat backend."""
    try:
        service = NewChatService()
        return await service.select_service(req.model_dump())
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except httpx.RequestError as exc:
        logger.error("New-chat backend unreachable: %s", exc)
        raise HTTPException(status_code=502, detail="New-chat backend is unreachable")


@router.post("/reset")
async def reset():
    """Proxy: reset the chat session on the new-chat backend."""
    try:
        service = NewChatService()
        return await service.reset()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except httpx.RequestError as exc:
        logger.error("New-chat backend unreachable: %s", exc)
        raise HTTPException(status_code=502, detail="New-chat backend is unreachable")


@router.post("/chat")
async def chat(
    req: ChatRequest,
    authorization: Optional[str] = Header(None),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Proxy: send a chat message to the new-chat backend."""
    try:
        user, tenant = user_and_tenant

        # Extract raw JWT from the "Authorization: Bearer <token>" header
        jwt_token = None
        if authorization and authorization.lower().startswith("bearer "):
            jwt_token = authorization.split(" ", 1)[1].strip()

        payload = {
            **req.model_dump(),
            "user_mst_code": user.code,
            "tenant_code": tenant.code,
            "jwt_token": jwt_token,
        }
        service = NewChatService()
        return await service.chat(payload)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except httpx.RequestError as exc:
        logger.error("New-chat backend unreachable: %s", exc)
        raise HTTPException(status_code=502, detail="New-chat backend is unreachable")


@router.post("/sessions")
async def list_sessions(
    authorization: Optional[str] = Header(None),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Proxy: list the user's recent chat sessions from the new-chat backend."""
    try:
        user, tenant = user_and_tenant

        jwt_token = None
        if authorization and authorization.lower().startswith("bearer "):
            jwt_token = authorization.split(" ", 1)[1].strip()

        payload = {
            "user_mst_code": user.code,
            "tenant_code": tenant.code,
            "jwt_token": jwt_token,
        }
        service = NewChatService()
        return await service.list_sessions(payload)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except httpx.RequestError as exc:
        logger.error("New-chat backend unreachable: %s", exc)
        raise HTTPException(status_code=502, detail="New-chat backend is unreachable")


@router.post("/sessions/select")
async def select_session(
    req: SelectSessionRequest,
    authorization: Optional[str] = Header(None),
    user_and_tenant: Tuple[UserMstModel, TenantsMstModel] = Depends(get_current_user_and_tenant),
):
    """Proxy: load full state for a single session from the new-chat backend."""
    try:
        user, tenant = user_and_tenant

        jwt_token = None
        if authorization and authorization.lower().startswith("bearer "):
            jwt_token = authorization.split(" ", 1)[1].strip()

        payload = {
            **req.model_dump(),
            "user_mst_code": user.code,
            "tenant_code": tenant.code,
            "jwt_token": jwt_token,
        }
        service = NewChatService()
        return await service.select_session(payload)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except httpx.RequestError as exc:
        logger.error("New-chat backend unreachable: %s", exc)
        raise HTTPException(status_code=502, detail="New-chat backend is unreachable")

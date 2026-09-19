"""
Pydantic schemas for model serving API requests and responses.
"""
from typing import Optional
from pydantic import BaseModel


# ── HuggingFace model validation ─────────────────────────────────────────────

class ValidateModelRequest(BaseModel):
    model_name: str
    hf_token: Optional[str] = None


class ValidateModelResponse(BaseModel):
    valid: bool
    gated: bool = False
    sha: Optional[str] = None
    error: Optional[str] = None


# ── Model registry (Jenkins-facing) ──────────────────────────────────────────

class MarkReadyRequest(BaseModel):
    model_id: str
    revision: str


class ModelStatusResponse(BaseModel):
    status: str
    error_message: Optional[str] = None

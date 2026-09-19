"""
Model Serving Service

Business logic for HuggingFace model validation and related model-serving operations.
"""
import logging
from typing import Optional

import httpx

from app.schemas.model_serving_schemas import ValidateModelResponse

logger = logging.getLogger(__name__)

_HF_API_BASE = "https://huggingface.co/api/models"


async def validate_hf_model(model_name: str, hf_token: Optional[str] = None) -> ValidateModelResponse:
    """Check whether a HuggingFace model is accessible, with or without a token.

    Flow:
    1. Try unauthenticated — catches public models quickly.
    2. On 401/403: model is gated or private.
       - If token provided, retry with it.
       - If no token, tell the user one is required.
    3. On 404: model does not exist.
    4. Any other status or network error: surface a clear message.

    Returns a ValidateModelResponse — never raises.
    """
    model_name = model_name.strip()
    if not model_name:
        return ValidateModelResponse(valid=False, error="Model name is required.")

    url = f"{_HF_API_BASE}/{model_name}"

    async def _get(token: Optional[str]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(timeout=10) as client:
            return await client.get(url, headers=headers)

    try:
        resp = await _get(None)

        if resp.status_code == 200:
            body = resp.json()
            sha = body.get("sha") or "main"
            # HF returns 200 for gated models but still marks them gated in the body.
            # The actual file downloads require token + license acceptance.
            is_gated = bool(body.get("gated"))
            if is_gated:
                if not hf_token:
                    return ValidateModelResponse(
                        valid=False,
                        gated=True,
                        error="This model is gated. Please provide a HuggingFace token and accept the license on HuggingFace.",
                    )
                # Verify the token actually has access by fetching with auth.
                token_resp = await _get(hf_token)
                if token_resp.status_code == 200:
                    return ValidateModelResponse(
                        valid=True,
                        gated=True,
                        sha=sha[:40] if len(sha) > 7 else sha,
                    )
                return ValidateModelResponse(
                    valid=False,
                    gated=True,
                    error=(
                        "Token does not have access to this model. "
                        "Ensure you have accepted the model license on HuggingFace."
                    ),
                )
            return ValidateModelResponse(
                valid=True,
                gated=False,
                sha=sha[:40] if len(sha) > 7 else sha,
            )

        if resp.status_code in (401, 403):
            if not hf_token:
                return ValidateModelResponse(
                    valid=False,
                    gated=True,
                    error="This model is gated or private. Please provide a HuggingFace token.",
                )
            token_resp = await _get(hf_token)
            if token_resp.status_code == 200:
                sha = token_resp.json().get("sha") or "main"
                return ValidateModelResponse(
                    valid=True,
                    gated=True,
                    sha=sha[:40] if len(sha) > 7 else sha,
                )
            return ValidateModelResponse(
                valid=False,
                gated=True,
                error=(
                    "Token does not have access to this model. "
                    "Ensure you have accepted the model license on HuggingFace."
                ),
            )

        if resp.status_code == 404:
            return ValidateModelResponse(
                valid=False,
                error=f'Model "{model_name}" not found on HuggingFace. Check the model name (format: org/model-name).',
            )

        return ValidateModelResponse(
            valid=False,
            error=f"HuggingFace API returned an unexpected status: {resp.status_code}.",
        )

    except httpx.TimeoutException:
        return ValidateModelResponse(
            valid=False,
            error="Request to HuggingFace timed out. Please try again.",
        )
    except Exception as exc:
        logger.warning("[HF validate] Unexpected error for model %s: %s", model_name, exc)
        return ValidateModelResponse(
            valid=False,
            error="Could not reach HuggingFace API. Please try again.",
        )

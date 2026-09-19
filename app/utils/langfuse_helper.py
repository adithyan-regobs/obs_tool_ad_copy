"""
Langfuse Helper - Sync logging logic for vector DB and Service Sage monitoring.

This helper contains the sync logging methods that run in thread pool.
The LangfuseService delegates to these methods.
"""
import logging
from typing import Dict, Any, Optional
from datetime import datetime

from app.core.config import settings

logger = logging.getLogger(__name__)


class LangfuseHelper:
    """Helper for Langfuse sync logging operations."""

    @staticmethod
    def log_service_sage_generation(
        client,
        user_query: str,
        ai_response: str,
        intent: str,
        chat_info_code: str,
        user_id: str,
        tenant_id: str,
        metadata: Dict[str, Any]
    ) -> None:
        """Log Service Sage interaction as a generation."""
        generation_metadata = {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "chat_code": chat_info_code,
            "intent": intent,
            "service": "service_sage",
            "timestamp": datetime.utcnow().isoformat(),
            **metadata
        }

        chat_input = [{"role": "user", "content": user_query}]

        with client.start_as_current_generation(
            name="service_sage_interaction",
            model=settings.openai_model,
            input=chat_input,
            output=ai_response,
            metadata=generation_metadata
        ) as generation:
            generation.update_trace(user_id=user_id, session_id=chat_info_code)

        client.flush()
        logger.debug(f"Logged Service Sage: intent={intent}")

    @staticmethod
    def log_llm_span(
        client,
        call_type: str,
        input_text: str,
        output_text: str,
        latency_ms: float,
        metadata: Dict[str, Any],
        usage: Dict[str, int] = None,
    ) -> None:
        """
        Log an LLM call as a Langfuse GENERATION (not a plain span) so the UI shows the
        model, token usage, and cost. `usage` is {"input": n, "output": n, "total": n}.
        """
        model = metadata.get("model") or settings.openai_model
        with client.start_as_current_generation(name=f"llm_{call_type}", model=model) as gen:
            gen.update(
                # Populate Langfuse's native Input/Output panels
                input=input_text,
                output=output_text,
                # Token usage → drives the Tokens + Cost columns in Langfuse
                usage_details=usage if usage else None,
                metadata={
                    "call_type": call_type,
                    "latency_ms": round(latency_ms, 2),
                    "model": model,
                    "timestamp": datetime.utcnow().isoformat(),
                    **metadata
                }
            )
        client.flush()
        logger.debug(f"Logged LLM call: {call_type}, {latency_ms:.1f}ms, usage={usage}")

    @staticmethod
    def log_embedding_span(
        client,
        operation_type: str,
        text_count: int,
        latency_ms: float,
        success: bool,
        metadata: Dict[str, Any]
    ) -> None:
        """Log embedding operation as a span."""
        with client.start_as_current_span(name=f"embedding_{operation_type}") as span:
            span.update(
                level="DEFAULT" if success else "ERROR",
                metadata={
                    "operation_type": operation_type,
                    "text_count": text_count,
                    "latency_ms": round(latency_ms, 2),
                    "success": success,
                    "model": settings.embedding_model,
                    "timestamp": datetime.utcnow().isoformat(),
                    **metadata
                }
            )
        client.flush()
        logger.debug(f"Logged embedding: {operation_type}, {text_count} texts")

    @staticmethod
    def log_vector_search_span(
        client,
        collection_name: str,
        query_text: str,
        result_count: int,
        top_score: Optional[float],
        latency_ms: float,
        metadata: Dict[str, Any]
    ) -> None:
        """Log vector search as a span."""
        with client.start_as_current_span(name="vector_search") as span:
            span.update(
                level="DEFAULT" if result_count > 0 else "WARNING",
                metadata={
                    "collection_name": collection_name,
                    "query_text": query_text[:100] if query_text else None,
                    "result_count": result_count,
                    "top_score": round(top_score, 4) if top_score else None,
                    "latency_ms": round(latency_ms, 2),
                    "timestamp": datetime.utcnow().isoformat(),
                    **metadata
                }
            )
        client.flush()
        logger.debug(f"Logged vector search: {result_count} results")

    @staticmethod
    def log_parameter_match_span(
        client,
        query: str,
        match_path: str,
        canonical_name: Optional[str],
        confidence: Optional[str],
        score: float,
        latency_ms: float,
        alternatives_count: int,
        metadata: Dict[str, Any]
    ) -> None:
        """Log parameter match as a span."""
        level = "WARNING" if confidence == "no_match" else "DEFAULT"

        with client.start_as_current_span(name="parameter_match") as span:
            span.update(
                level=level,
                metadata={
                    "query": query[:200] if query else None,
                    "match_path": match_path,
                    "canonical_name": canonical_name,
                    "confidence": confidence,
                    "score": round(score, 4) if score else 0,
                    "latency_ms": round(latency_ms, 2),
                    "alternatives_count": alternatives_count,
                    "timestamp": datetime.utcnow().isoformat(),
                    **metadata
                }
            )
        client.flush()
        logger.debug(f"Logged param match: {match_path}, {confidence}")

"""
Workflow Trail Utility for tracking workflow execution metadata.

Provides helper functions to append type-safe entries to the workflow trail
for audit logging and chat history context.
"""
from datetime import datetime, timezone
from typing import Optional, List
from langchain_openai import ChatOpenAI

from app.infra_chat_agent.chat_state import WorkflowTrailEntry


def append_to_workflow_trail(
    workflow_trail: List[WorkflowTrailEntry],
    phase: str,
    llm_purpose: str,
    message: str,
    reasoning: str,
    extraction_method: str,
    llm_model: Optional[str] = None,
    confidence: Optional[float] = None,
) -> List[WorkflowTrailEntry]:
    """
    Append a new entry to the workflow trail.

    Args:
        workflow_trail: Existing workflow trail list
        phase: Workflow phase (e.g., "intent_detection", "parameter_extraction", "qa_execution")
        llm_purpose: Purpose of LLM call (e.g., "intent_detection", "parameter_extraction", "qa_generation")
        message: Human-readable message describing what happened
        reasoning: LLM reasoning/explanation or validation result
        extraction_method: Method used ("llm", "deterministic", "manual")
        llm_model: LLM model name (optional, None for deterministic methods)
        confidence: LLM confidence score 0.0-1.0 (optional, mainly for intent detection)

    Returns:
        Updated workflow trail list with new entry appended
    """
    entry: WorkflowTrailEntry = {
        "phase": phase,
        "llm_purpose": llm_purpose,
        "llm_model": llm_model,
        "confidence": confidence,
        "reasoning": reasoning,
        "extraction_method": extraction_method,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    workflow_trail.append(entry)
    return workflow_trail


def append_llm_execution_to_trail(
    workflow_trail: List[WorkflowTrailEntry],
    phase: str,
    llm_purpose: str,
    llm_client: ChatOpenAI,
    message: str,
    reasoning: str,
    confidence: Optional[float] = None,
) -> List[WorkflowTrailEntry]:
    """
    Append an LLM execution entry to the workflow trail.

    Convenience function that automatically extracts the model name from the LLM client.

    Args:
        workflow_trail: Existing workflow trail list
        phase: Workflow phase (e.g., "intent_detection", "parameter_extraction", "qa_execution")
        llm_purpose: Purpose of LLM call (e.g., "intent_detection", "parameter_extraction", "qa_generation")
        llm_client: ChatOpenAI client instance (model name will be extracted)
        message: Human-readable message describing what happened
        reasoning: LLM reasoning/explanation
        confidence: LLM confidence score 0.0-1.0 (optional, mainly for intent detection)

    Returns:
        Updated workflow trail list with new entry appended
    """
    return append_to_workflow_trail(
        workflow_trail=workflow_trail,
        phase=phase,
        llm_purpose=llm_purpose,
        message=message,
        reasoning=reasoning,
        extraction_method="llm",
        llm_model=llm_client.model_name,
        confidence=confidence
    )


def append_deterministic_execution_to_trail(
    workflow_trail: List[WorkflowTrailEntry],
    phase: str,
    llm_purpose: str,
    message: str,
    reasoning: str,
) -> List[WorkflowTrailEntry]:
    """
    Append a deterministic (non-LLM) execution entry to the workflow trail.

    Use this for validation, rule-based processing, or any non-LLM operations.

    Args:
        workflow_trail: Existing workflow trail list
        phase: Workflow phase (e.g., "validation", "rbac_check")
        llm_purpose: Purpose description (e.g., "validation", "policy_check")
        message: Human-readable message describing what happened
        reasoning: Explanation of the deterministic result

    Returns:
        Updated workflow trail list with new entry appended
    """
    return append_to_workflow_trail(
        workflow_trail=workflow_trail,
        phase=phase,
        llm_purpose=llm_purpose,
        message=message,
        reasoning=reasoning,
        extraction_method="deterministic",
        llm_model=None,
        confidence=None
    )

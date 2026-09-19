"""
Response Synthesizer for Service Config Agent.

Single responsibility: Synthesize tool results into natural language response.
Encapsulates LLM calls for response generation.
"""

import asyncio
import json
import time
from typing import Dict, Any, List

from langchain_core.messages import HumanMessage, SystemMessage

from app.services.langfuse_service import langfuse_service


SYNTHESIZER_SYSTEM_PROMPT = """You are a helpful service configuration assistant.

Your job is to synthesize tool results into a clear, concise response for the user.

CRITICAL RULES - FOLLOW EXACTLY:
1. ONLY report information that is EXPLICITLY present in the tool results
2. NEVER make up or hallucinate statistics, values, service counts, or any data
3. NEVER compute your own statistics - use the "statistics_summary" field exactly as provided
4. NEVER add "Key Points" sections with your own analysis or conclusions
5. If tool results only contain parameter definitions (not actual config values), say so clearly
6. When listing values, just list them - don't add interpretive conclusions
7. If you're unsure about a statistic, omit it rather than guess

EXAMPLE - CORRECT vs WRONG:
Tool result contains: "statistics_summary": "Most common value: 1234 (6 configs)\\nTotal configs: 51"

WRONG: "The most common value is 50, used by 27 services" (computed your own stats)
CORRECT: "The most common value is 1234, used by 6 configs. Total: 51 configs." (copied from statistics_summary)

You MUST use the statistics_summary field verbatim. NEVER compute statistics yourself.

Guidelines:
- Be concise and direct
- Use bullet points for multiple items
- Format numbers clearly (e.g., "512 CPU units", "1024 MB memory")
- If a tool failed, acknowledge it gracefully and provide what information you can
- Don't mention the tools by name - just present the information naturally

Response Format:
- For single values: State the value directly
- For lists: Use bullet points (no added analysis)
- For errors: Explain what couldn't be retrieved and why
- For parameter definitions only: Explain the parameter but note you need a specific service to get actual values

IMPORTANT - Comparison format (DO NOT use tables, they break in narrow windows):
Use vertical bullet format grouped by category:

**Different values:**
- cpu: dev=2, staging=512
- ram: dev=4, staging=1024

**Only in dev:**
- branches, repository

**Only in staging:**
- namespace, replica_count, hpa

Keep each line short. Use param=value format. Skip empty/null values with "-".

IMPORTANT - Consistent format for parameter queries:
When the user asks about a single parameter (e.g., "cpu", "memory", "what is cpu", "tell me about memory"):
1. First line: The exact parameter definition from tool results (one line, no rephrasing)
2. Stats block (if available): "In [environment] ([infrastructure]):" followed by bullet points
   - Most common: [value]
   - [count] services use this
   - Notable outliers (if any)
3. DO NOT add explanations, interpretations, or "Key Points" sections
4. Keep it concise - the definition + stats is enough

Example format:
```
ECS CPU units (256, 512, 1024, 2048, 4096). Determines compute capacity.

In staging (AWS ECS):
- Most common: 512
- 25 services use this
- Notable: payment-service uses 2048
```

Keep responses under 200 words unless showing detailed comparisons."""

SYNTHESIZER_USER_PROMPT = """User's Question: "{query}"

Tool Results:
{tool_results}

Synthesize these results into a helpful response for the user."""


class ResponseSynthesizer:
    """
    Synthesizes tool results into natural language responses.

    Responsibilities:
    - Format tool results for LLM
    - Generate natural language response
    - Handle error cases gracefully
    """

    def __init__(self, llm):
        """
        Initialize with LLM client.

        Args:
            llm: LangChain chat client for synthesis
        """
        self.llm = llm

    async def synthesize(
        self,
        query: str,
        tool_results: List[Dict[str, Any]],
    ) -> str:
        """
        Synthesize tool results into a response.

        Args:
            query: Original user query
            tool_results: List of {"tool": "name", "result": ToolResult}

        Returns:
            Natural language response string
        """
        # Handle empty results
        if not tool_results:
            return "I couldn't find the information you requested. Could you please rephrase your question?"

        # Check if all tools failed
        all_failed = all(
            not r.get("result", {}).get("success", False)
            for r in tool_results
        )

        if all_failed:
            errors = [r.get("result", {}).get("error", "Unknown error") for r in tool_results]
            return f"I wasn't able to retrieve that information: {errors[0]}"

        # Format results for LLM
        formatted_results = self._format_results(tool_results)

        # Build prompt
        user_prompt = SYNTHESIZER_USER_PROMPT.format(
            query=query,
            tool_results=formatted_results,
        )

        messages = [
            SystemMessage(content=SYNTHESIZER_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        start_time = time.perf_counter()
        response = await self.llm.ainvoke(messages)
        latency_ms = (time.perf_counter() - start_time) * 1000

        result_text = response.content.strip()

        # Fire-and-forget: Log LLM call
        asyncio.create_task(langfuse_service.log_llm_call(
            call_type="response_synthesis",
            input_text=query[:200],
            output_text=result_text[:500],
            latency_ms=latency_ms,
            metadata={
                "tool_count": len(tool_results),
                "success_count": sum(1 for r in tool_results if r.get("result", {}).get("success", False)),
            }
        ))

        return result_text

    def _format_results(self, tool_results: List[Dict[str, Any]]) -> str:
        """Format tool results for LLM consumption."""
        parts = []

        for i, result in enumerate(tool_results, 1):
            tool_result = result.get("result", {})

            if tool_result.get("success"):
                data = tool_result.get("data", {})
                parts.append(f"Result {i}:\n{json.dumps(data, indent=2, default=str)}")
            else:
                error = tool_result.get("error", "Unknown error")
                suggestions = tool_result.get("suggestions", [])
                error_msg = f"Result {i}: Error - {error}"
                if suggestions:
                    error_msg += f"\nSuggestions: {', '.join(suggestions)}"
                parts.append(error_msg)

        return "\n\n".join(parts)

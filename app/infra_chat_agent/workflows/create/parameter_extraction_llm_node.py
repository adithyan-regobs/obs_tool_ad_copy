"""
Parameter extraction LLM node for CREATE workflow.

This node extracts attribute parameters from user messages using LLM.
Placement parameters are assumed to be already collected (from external source).
"""
import json
import logging
from typing import Any, Dict, List

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.utils.graph_utils import get_tenant_id
from app.infra_chat_agent.utils.llm_integration_util import get_llm_client
from app.infra_chat_agent.config.config_models import (
    TenantId,
    InfraTypeCode,
    ParameterMeta,
    ParamType,
    StaticSource,
)
from app.infra_chat_agent.config.resource_meta_repo import resource_meta_repo
from app.infra_chat_agent.config.prompt_builder import DefaultPromptBuilder
from app.services.langfuse_service import langfuse_service

logger = logging.getLogger(__name__)


def _build_attributes_schema(attributes: List[ParameterMeta]) -> List[Dict[str, Any]]:
    """
    Convert ParameterMeta list to LLM-friendly schema.

    Args:
        attributes: List of ParameterMeta from attributes group

    Returns:
        List of schema dictionaries for LLM context
    """
    schema = []

    for param in attributes:
        param_dict = {
            "key": param.key,
            "name": param.name,
            "type": param.type.value,
            "required": param.required,
        }

        # Add description if present
        if param.description:
            param_dict["description"] = param.description

        # Add validation rules
        if param.validation:
            if param.validation.allowed:
                param_dict["allowed_values"] = param.validation.allowed
            if param.validation.regex:
                param_dict["regex_pattern"] = param.validation.regex

        # Add default value
        if param.default is not None:
            param_dict["default"] = param.default

        # Add examples if present
        if param.examples:
            param_dict["examples"] = param.examples

        # Add value source info for context
        if param.value_source and isinstance(param.value_source, StaticSource):
            param_dict["static_options"] = [
                {"label": opt.label, "value": opt.value}
                for opt in param.value_source.options
            ]

        schema.append(param_dict)

    return schema


def _build_ambiguous_input_examples(
    display_name: str,
    attributes: List[ParameterMeta],
) -> str:
    """
    Build dynamic examples for ambiguous single-word/short input extraction.

    Uses the first required STRING parameter (typically identifier) and its examples
    to generate resource-specific extraction examples.

    Args:
        display_name: Resource display name (s3, sqs, dynamodb, etc.)
        attributes: List of ParameterMeta from attributes group

    Returns:
        Formatted string with extraction examples for the prompt
    """
    examples = []

    # Find required STRING parameters sorted by order (first is typically identifier)
    string_params = [
        p for p in sorted(attributes, key=lambda x: x.order)
        if p.type == ParamType.STRING and p.required
    ]

    if not string_params:
        return ""

    first_param = string_params[0]
    display_upper = display_name.upper()

    # Build single-word examples from the first param's examples or use generic ones
    single_word_examples = []
    if first_param.examples:
        # Get clean examples (without arrows) and extract single words
        for ex in first_param.examples[:3]:
            if "→" not in str(ex):
                # Normalize to get the base value
                normalized = str(ex).lower().replace(" ", "-").replace("_", "-")
                # Create a simple single-word version if possible
                parts = normalized.split("-")
                if parts:
                    single_word_examples.append(parts[0])

    # Ensure we have at least some examples
    if not single_word_examples:
        single_word_examples = ["user", "data", "logs"]

    # Build examples for single-word input (most common issue)
    for word in single_word_examples[:2]:
        examples.append(
            f"    - Example for {display_upper} (only {first_param.key} missing): "
            f'"{word}" → {first_param.key}="{word}"'
        )

    # Build multi-word example if we have a second STRING param
    if len(string_params) >= 2:
        second_param = string_params[1]
        # Example showing order priority when multiple params missing
        examples.append(
            f"    - Example for {display_upper} ({first_param.key}=collected, {second_param.key}=missing): "
            f'"some value" → {second_param.key}="some-value"'
        )

    return "\n".join(examples)


def _build_extraction_prompt(
    *,
    display_name: str,
    attributes_schema: List[Dict[str, Any]],
    placement_params: Dict[str, Any],
    collected_attributes: Dict[str, Any],
    remaining_params: Dict[str, Any],
    user_message: str,
    param_descriptions: Dict[str, str],
    param_metadata: List,
) -> str:
    """
    Build the system prompt for LLM attribute extraction.

    Args:
        display_name: Resource display name for prompts (s3, sqs, dynamodb, etc.)
        attributes_schema: Schema of attributes to extract
        placement_params: Already collected placement parameters
        collected_attributes: Already collected attribute parameters
        remaining_params: Missing required parameters that still need to be collected
        user_message: Current user message
        param_descriptions: Human-readable descriptions from DefaultPromptBuilder
        param_metadata: List of ParameterMeta objects for building examples

    Returns:
        System prompt for LLM extraction
    """

    # Build placement context section
    placement_section = ""
    if placement_params:
        placement_list = [f"{k}: {v}" for k, v in placement_params.items()]
        placement_section = f"""
### Already Collected (Placement):
The following placement parameters are already known:
{chr(10).join(f'  - {item}' for item in placement_list)}

"""

    # Build collected attributes context section
    collected_attributes_section = ""
    if collected_attributes:
        collected_list = []
        for k, v in collected_attributes.items():
            if isinstance(v, list):
                # Format list values
                formatted = ", ".join(str(item) for item in v)
                collected_list.append(f"{k}: [{formatted}]")
            else:
                collected_list.append(f"{k}: {v}")

        collected_attributes_section = f"""
### Already Collected (Attributes):
The following attribute parameters have already been collected:
{chr(10).join(f'  - {item}' for item in collected_list)}

"""

    # Build remaining/missing parameters section - CRITICAL for single-word extraction
    remaining_params_section = ""
    remaining_keys = list(remaining_params.keys()) if remaining_params else []

    # ALWAYS include extraction guidance (for both new extraction AND updates)
    if not collected_attributes and len(remaining_keys) == 1:
        # Simple case: first value, only one missing param
        extraction_guidance = f"""
🎯 **SINGLE VALUE RULE**: Since there is exactly ONE missing required parameter ({remaining_keys[0]})
   and NO attributes have been collected yet, if the user provides a single word or short phrase,
   assign it to {remaining_keys[0]}.

   Example: User says "user" → Extract: {remaining_keys[0]}="user"
"""
    else:
        # Complex case: some params collected OR all params collected (update mode)
        extraction_guidance = """
🔍 **SEMANTIC MATCHING RULE**: Use semantic analysis to determine what the user is providing:
   1. **FIRST (HIGHEST PRIORITY)** - Check if user is explicitly naming a parameter
      → When matching, prefer EXACT name match over partial/superset match
      → User input "X" matches parameter named "X", NOT parameter named "X Y" or "X Z"
      → If exact name match found, extract ONLY to that parameter
      → IGNORE value type (ENUM/STRING/BOOL) for explicitly named parameters
   2. **SECOND** - Check if value matches any MISSING required parameter (name, description, type)
   3. **THIRD** - For STANDALONE values only (no explicit param name), check EXACT ENUM matching
      → Only apply when user did NOT explicitly name a parameter
      → EXACT match required: "S" matches [S,N,B] ✅, "as" does NOT match [S,N,B] ❌
      → Non-matching values like "as" go to MISSING STRING params instead
   4. Use parameter TYPE to guide matching (STRING identifiers, BOOL values, ENUM options)
   5. If ambiguous between multiple STRING params, prefer the FIRST MISSING by order

   🔴 **CRITICAL**: Explicit parameter naming ALWAYS takes priority!
   - If user names a parameter, extract to THAT parameter regardless of value type
   - ENUM matching only applies to STANDALONE values without explicit param names
"""

    # Build the section - include guidance even if no remaining params
    if remaining_keys:
        remaining_params_section = f"""
### ⚠️ MISSING REQUIRED PARAMETERS:
The following required parameters are STILL MISSING and need to be extracted:
{chr(10).join(f'  - **{key}**' for key in remaining_keys)}
{extraction_guidance}
"""
    else:
        # All params collected - still include update guidance
        remaining_params_section = f"""
### ✅ ALL REQUIRED PARAMETERS COLLECTED
All required parameters have been collected. User may be providing CORRECTIONS or UPDATES.
{extraction_guidance}
"""

    # Build attributes descriptions section
    attributes_section = "### Attributes to Extract:\n"
    for param_name, desc in param_descriptions.items():
        attributes_section += f"\n**{param_name}**:\n{desc}\n"

    # Build concrete extraction examples from parameter metadata
    # BALANCED: Cover ALL params (variations limited in prompt_builder)
    all_extraction_examples = []
    for param in param_metadata:  # Cover ALL parameters
        param_examples = DefaultPromptBuilder.build_parameter_extraction_examples(
            param=param,
            max_examples=3  # 3 base examples, 1 variation each = ~6 per param
        )
        if param_examples:
            all_extraction_examples.append(f"\n   **{param.name}** (key: {param.key}):")
            all_extraction_examples.extend(param_examples)

    # Build multi-value extraction examples
    multi_value_examples = DefaultPromptBuilder.build_multi_value_extraction_examples(
        params=param_metadata[:5],
        max_examples=2
    )
    multi_value_section = ""
    if multi_value_examples:
        multi_value_section = f"""

### MULTI-VALUE EXTRACTION (extract ALL values from a single message):
Users often provide multiple values in one message. Extract ALL of them:
{"".join(multi_value_examples)}
"""

    # Build explicit entry examples (Name: value format - NO typo correction)
    explicit_entry_examples = DefaultPromptBuilder.build_explicit_entry_examples(
        params=param_metadata,
        max_examples=6
    )
    explicit_entry_section = ""
    if explicit_entry_examples:
        explicit_entry_section = f"""

### EXPLICIT NAME:VALUE FORMAT (NO TYPO CORRECTION - user is being intentional):
When user provides input in "Parameter Name: value" format, extract EXACTLY as provided:
{chr(10).join(explicit_entry_examples)}
"""

    extraction_examples_section = ""
    if all_extraction_examples:
        extraction_examples_section = f"""
### EXTRACTION EXAMPLES (from actual {display_name.upper()} parameters):
These examples show how to extract parameter values from various user input formats:
{"".join(all_extraction_examples[:60])}{multi_value_section}{explicit_entry_section}

**Key Patterns:**
- **Accept ANY value that looks like a name/identifier** - even a single word, short names, casual names, technical names
- **Accept ALL format variations**: spaces, hyphens, underscores, mixed case, UPPERCASE, PascalCase, lowercase
- **Accept typos and irregular spelling** - don't reject values because they look informal or have typos
- **Normalize identifier parameters (names)** to lowercase-hyphenated format
- **Normalize ENUM parameters** to match value_source canonical format
- Match parameter by semantic meaning (using Name, Description, Examples)
- Use parameter KEY in response, not Name
- **Be VERY liberal in acceptance** - validation happens later
"""

    # Build dynamic ambiguous input examples based on resource metadata
    ambiguous_input_examples = _build_ambiguous_input_examples(display_name, param_metadata)

    # Build JSON schema for LLM
    schema_json = json.dumps(attributes_schema, indent=2)

    return f"""You are an ATTRIBUTE parameter extractor for AWS {display_name.upper()} resource creation.

IMPORTANT: You extract ONLY ATTRIBUTES, NOT placement parameters.
Placement parameters (vendor, environment, geo location, etc.) are already collected and provided for context only.

{placement_section}{collected_attributes_section}{remaining_params_section}{attributes_section}
{extraction_examples_section}

### Attribute Schema (ONLY extract parameters from this schema):
```json
{schema_json}
```

### User Message:
"{user_message}"

### Your Task:
Extract ATTRIBUTE values EXPLICITLY mentioned in the user message.
ONLY extract parameters that are listed in the "Attribute Schema" above.
Respond ONLY with valid JSON using the parameter KEY (not name):

```json
{{
  "slot_updates": {{
    "parameter_key": "extracted_value"
  }}
}}
```

### CRITICAL RULES:

**⚠️ TYPO CORRECTION (ONLY FOR STANDALONE VALUES) ⚠️**
When extracting identifier/name values from **STANDALONE INPUT** (no explicit parameter name), you may fix obvious spelling mistakes.

🚫 **NEVER correct when user uses EXPLICIT FORMAT** (parameter name + value):
- If user provides "ParamName: value" or "ParamName = value" format, PRESERVE the value EXACTLY as typed
- The user is being intentional with explicit format - DO NOT "fix" their values
See "TYPO CORRECTIONS" examples in EXTRACTION EXAMPLES section above for standalone value corrections only.

1. ONLY extract ATTRIBUTE parameters from the "Attribute Schema" above
2. NEVER extract placement parameters (infra_vendor_enum, environment_enum, geo_loc_mst_code, etc.)
3. ONLY extract values EXPLICITLY mentioned in the user message
4. DO NOT hallucinate values that are not mentioned

5. **MAPPING RULE - Name to Key (CRITICAL):**
   - Each parameter has a KEY (technical field name) and a Name (human-readable label)
   - Users will mention the Name (human-readable), NOT the Key
   - When user mentions a parameter by its Name, you MUST map it to its KEY
   - ALWAYS use the KEY as the dictionary key in your response, NEVER use the Name
   - Refer to schema for key-name mappings

6. **LIBERAL EXTRACTION & NORMALIZATION RULE (CRITICAL):**
   - **Accept ANY value that semantically matches a parameter** - short names, long names, casual names, technical names
   - **Accept ALL format variations**: spaces, hyphens, underscores, mixed case, UPPERCASE, lowercase, PascalCase
   - **Accept typos, misspellings, and irregular formatting** in both parameter names AND values
   - **FIX OBVIOUS TYPOS** - see "TYPO CORRECTIONS" in EXTRACTION EXAMPLES section
   - **DO NOT reject values** because they:
     * Are too short (even 2-3 character values like "ls", "id", "pk", "db" are completely valid)
     * Look like Unix commands (e.g., "ls", "cd", "rm" are valid parameter values)
     * Look informal or casual
     * Have irregular spacing or capitalization
     * Don't match examples exactly
     * Don't match expected format/regex (validation handles that)
   - **Normalize values based on parameter type** - see EXTRACTION EXAMPLES above for exact patterns:
     * Identifier parameters (names, buckets, queues, tables): normalize to lowercase-hyphenated AND fix obvious typos
     * ENUM parameters: normalize to match value_source canonical format
     * BOOL parameters: normalize to lowercase true/false
     * Other STRING parameters: extract as provided
   - **When in doubt, EXTRACT IT** - validation happens later, your job is to capture values liberally

7. Match extracted values to the correct parameter using semantic analysis:
   - Look at parameter's Name, Description, and Examples to understand what it represents
   - Match user phrases/words to the parameter's semantic meaning
   - Once you identify which parameter the user is referring to, use its KEY
   - Extract and normalize the value following the patterns shown in EXTRACTION EXAMPLES

8. For ENUM types: Normalize to lowercase - see EXTRACTION EXAMPLES for pattern
9. For STRING identifier types: Normalize to lowercase-hyphenated - see EXTRACTION EXAMPLES for pattern
10. For BOOL types: Normalize to lowercase true/false - see EXTRACTION EXAMPLES for pattern
11. For INT/NUMBER types: Extract numeric values as-is
12. For JSON type parameters (lists): Extract values (comma-separated string or list)
13. Return empty object `{{}}` if no attributes are mentioned

14. **AMBIGUOUS INPUT & ORDER PRIORITY (CRITICAL):**
    - When input could match MULTIPLE STRING parameters (e.g., both identifier and partition_key accept strings)
    - Use **ORDER PRIORITY**: Assign to the FIRST MISSING REQUIRED parameter by order (order field in schema)
    - Infrastructure keywords in values are VALID: "dlf service", "api queue", "user table" are all valid names
    - Words like "service", "queue", "table", "api", "data" are part of the VALUE, not meta-commands
{ambiguous_input_examples}

15. **TYPO CORRECTION (STANDALONE VALUES ONLY):**
    - When extracting identifier/name values from STANDALONE input, you may fix obvious spelling mistakes
    - NEVER correct values when user uses explicit format (ParamName: value)
    - See "TYPO CORRECTIONS" examples in EXTRACTION EXAMPLES section above
    - This applies only to standalone values without explicit parameter names

16. **REGEX/PATTERN PARAMETERS (Do NOT normalize):**
    - For parameters with regex/pattern validation (like Kong routes)
    - Do NOT add format prefixes/suffixes (like `~/` or `$`)
    - Extract the user's input AS-IS without any normalization
    - Validation will happen in the next step and will reject invalid formats
    - Example: user says "wewv" → extract as "wewv" (NOT "~/wewv$")
    - Example: user says "/api/users" → extract as "/api/users" (NOT "~/api/users$")

17. **VERY SHORT VALUES ARE VALID (CRITICAL):**
    - Accept values as short as 1-2 characters (e.g., "ls", "id", "pk", "a", "db")
    - Do NOT confuse short values with Unix commands - they are VALID parameter values
    - Do NOT try to "correct" or expand short values (don't change "ls" to "list")
    - Examples:
      * "partition key: ls" → partition_key="ls" (NOT ignored, NOT expanded)
      * "key = id" → partition_key="id"
      * "name: db" → identifier="db"
      * "table: a" → identifier="a"

18. **RESOURCE KEYWORDS ARE NOT VALUES (CRITICAL):**
    - If the user message is essentially just the resource type name, do NOT extract it as a parameter
    - These are INTENT TRIGGERS, not parameter values:
      * "dynamo", "dynamodb", "dynamo db" → just intent trigger, NOT table name
      * "s3", "bucket", "s3 bucket" → just intent trigger, NOT bucket name
      * "sqs", "queue", "sqs queue" → just intent trigger, NOT queue name
      * "sns", "topic", "sns topic" → just intent trigger, NOT topic name
      * "kong", "gateway", "kong gateway" → just intent trigger, NOT gateway name
    - Only extract as parameter if there's ADDITIONAL context beyond the resource keyword
    - Examples:
      * "dynamo" → {{}} (empty, just intent)
      * "dynamodb users" → {{"identifier": "users"}} (has additional word)
      * "create s3" → {{}} (empty, just intent)
      * "s3 logs-bucket" → {{"identifier": "logs-bucket"}} (has additional word)

19. **RESPECT EXPLICIT PARAMETER NAME - USER'S PARAMETER NAME TAKES ABSOLUTE PRIORITY (CRITICAL):**
    - When user mentions a parameter by name in ANY format, extract to that EXACT parameter
    - Formats include: `Name: value`, `Name = value`, `Name value`, or natural language statements
    - Natural language patterns:
      * "X should be Y", "set X to Y", "change X to Y", "X is Y", "make X Y"
      * "i want X to be Y", "X needs to be Y", "update X to Y"
    - **CRITICAL**: If parameter A and parameter B have similar names, and user explicitly mentions parameter A with a value, you MUST extract to parameter A - even if that value happens to be a valid ENUM/option for parameter B. The user named parameter A, NOT parameter B. NEVER reassign based on value matching when user explicitly named a parameter.
    - The PARAMETER NAME the user provides takes ABSOLUTE PRIORITY over value semantics
    - Do NOT reassign to a different parameter just because the value "looks like" it belongs elsewhere
    - Do NOT apply typo correction to explicit values - the user knows what they want
    - Match the user's parameter name to the closest matching parameter Name in the schema, then use that parameter's KEY
    - The user is being EXPLICIT and INTENTIONAL - respect their exact parameter choice verbatim

20. **DON'T VALIDATE OR INTERPRET DURING EXTRACTION (CRITICAL):**
    - Your job is EXTRACTION, not VALIDATION or INTERPRETATION
    - Do NOT reject values because they don't match expected format/regex
    - Do NOT reject values because they don't look like examples
    - Do NOT interpret or transform values to "help" - extract EXACTLY what the user provided
    - Do NOT guess what the user "meant" - if they said "2", extract "2" not "N"
    - Do NOT map numeric inputs to ENUM values (e.g., don't convert "2" to "N" for Number)
    - Extract the value AS-IS and let the validation step handle format checking and error messages
    - If the value is invalid, validation will tell the user - that's not your job

21. **ALWAYS EXTRACT PARAMETER UPDATES (CRITICAL):**
    - When user explicitly names a parameter with its value, ALWAYS extract it
    - This applies EVEN when there are MISSING required parameters
    - The user is CORRECTING or UPDATING an already-collected parameter
    - Do NOT ignore updates just because a DIFFERENT required parameter is missing
    - If the user explicitly provides "ParamName: value" or "param_name value", extract it regardless of what else is missing
    - Do NOT return empty {{}} just because the user didn't provide a missing required param
    - The parameter extraction LLM extracts what the user PROVIDED, not what is MISSING

### Extraction Philosophy:
- Your job is EXTRACTION based on semantic meaning, NOT validation
- Users speak in natural language using parameter Names (human-readable labels)
- Your output MUST use parameter Keys (technical field names)
- Match user phrases to parameters using Name, Description, and Examples
- Extract values liberally if they semantically match a parameter
- Validation (format, regex, allowed values) happens in the next step
- Better to extract and let validation catch errors than to skip values

### Extraction Pattern:
1. Read user message and identify which parameter they're referring to (by matching against Name, Description, Examples)
2. Once identified, look up that parameter's KEY
3. Extract the value from user message
4. Return {{"parameter_key": "extracted_value"}} using the KEY
"""


def _parse_llm_response(raw_response: str) -> Dict[str, Any]:
    """
    Parse LLM response, handling markdown code blocks.

    Args:
        raw_response: Raw LLM response string

    Returns:
        Dictionary with slot_updates
    """
    raw = raw_response.strip()

    # Handle markdown code blocks
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    raw = raw.strip().rstrip("`")

    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("Response is not a dict")

        return parsed.get("slot_updates", {})

    except json.JSONDecodeError as e:
        logger.error(f"[PARAM_EXTRACTION] Failed to parse LLM response: {e}")
        logger.debug(f"[PARAM_EXTRACTION] Raw response: {raw_response}")
        return {}


def parameter_extraction_llm_node(state: ChatState, config) -> ChatState:
    """
    Extract attribute parameters using LLM for CREATE workflow.

    Only extracts attributes (placement params come from request).

    Returns slot_parameters extracted from current user message.
    """

    tenant_id: str = get_tenant_id(state)
    user_message: str = (
        state.get("validated_user_message")
        or state.get("user_message", "")
    ).strip()
    resource_type: str = state.get("turn_resource", "")

    if not user_message or not resource_type:
        return {"slot_parameters": {}}

    try:
        # Get resource metadata from repo
        resource_meta = resource_meta_repo.get(
            tenant_id=TenantId(tenant_id),
            infra_type=InfraTypeCode(resource_type)
        )

        if not resource_meta:
            logger.error(
                f"[PARAM_EXTRACTION] ResourceMeta not found for "
                f"tenant={tenant_id}, infra_type={resource_type}"
            )
            return {"slot_parameters": {}}

        # Get display name for prompts
        display_name = resource_meta.infra_display_name

        # Get attributes parameters only
        attributes = resource_meta.attributes.parameters

        if not attributes:
            logger.info("[PARAM_EXTRACTION] No attributes to extract")
            return {"slot_parameters": {}}

        # Build schema for LLM
        attributes_schema = _build_attributes_schema(attributes)

        # Build parameter descriptions using DefaultPromptBuilder
        param_descriptions = {}
        for param in attributes:
            param_descriptions[param.name] = (
                DefaultPromptBuilder.build_param_description(
                    param=param,
                    include_hints=True
                )
            )

        # Get placement parameters (from collected_placement_parameters)
        placement_params = state.get("collected_placement_parameters") or {}

        # Get already collected attribute parameters (from collected_parameters)
        # NOTE: collected_parameters now contains ONLY attributes, not placement
        collected_attributes = state.get("collected_parameters") or {}

        # Get remaining/missing parameters for context
        remaining_params = state.get("remaining_parameters") or {}

        # Build extraction prompt (use display_name for user-friendly prompts)
        system_prompt = _build_extraction_prompt(
            display_name=display_name,
            attributes_schema=attributes_schema,
            placement_params=placement_params,
            collected_attributes=collected_attributes,
            remaining_params=remaining_params,
            user_message=user_message,
            param_descriptions=param_descriptions,
            param_metadata=attributes,  # Pass full parameter metadata for example generation
        )

        # Debug logging
        logger.info(f"[PARAM_EXTRACTION] Extracting attributes for {resource_type}")
        logger.debug(f"[PARAM_EXTRACTION] Attributes to extract: {[p.key for p in attributes]}")
        logger.debug(f"[PARAM_EXTRACTION] Placement params: {placement_params}")
        logger.debug(f"[PARAM_EXTRACTION] Collected attributes: {collected_attributes}")
        logger.debug(f"[PARAM_EXTRACTION] Remaining params: {remaining_params}")

        # Get LLM client - hardcoded to gpt-4o for better extraction accuracy
        intent = state.get("turn_intent", "CREATE")
        llm: ChatOpenAI = get_llm_client(tenant_id, intent, temperature=0.0, model_override="gpt-4o")

        # Get Langfuse callback handler
        user_id = state.get("user_id", "anonymous")
        langfuse_handler = langfuse_service.get_callback_handler(
            trace_name="parameter_extraction_llm"
        )

        # Invoke LLM with callback
        invoke_config = {}
        if langfuse_handler:
            invoke_config["callbacks"] = [langfuse_handler]
            # Add metadata via config (Langfuse v3 pattern)
            invoke_config["metadata"] = {
                "langfuse_user_id": user_id,
                "tenant_id": tenant_id,
                "resource_type": resource_type,
                "intent": intent,
                "placement_params": placement_params,
                "collected_attributes": collected_attributes,
            }
        
        llm_response = llm.invoke([SystemMessage(content=system_prompt)], config=invoke_config)

        # Parse response
        slot_updates = _parse_llm_response(llm_response.content or "")

        logger.info(f"[PARAM_EXTRACTION] Extracted: {slot_updates}")

        return {
            "parameter_extraction_method": "llm_success",
            "slot_parameters": slot_updates,
        }

    except Exception as e:
        logger.error(f"[PARAM_EXTRACTION] Failed: {str(e)}", exc_info=True)
        return {"slot_parameters": {}}

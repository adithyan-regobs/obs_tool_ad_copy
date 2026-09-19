"""
Create Flow V2 node with LLM and bound MCP tools.

This is the v2 CREATE flow that supports MCP tools for resource creation.
Initially supports database creation, but designed to be extensible for future tools.

Follows the same pattern as reference_node.py:
- LLM.bind_tools() for tool selection
- ToolNode for tool execution (in graph)
- Loop pattern: LLM → tool_node → LLM → response_handler

This is a CREATE flow variant that uses MCP tools instead of the traditional
parameter extraction → policy validation → response flow.
"""
import logging
import re
from typing import List

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, BaseMessage, ToolMessage, RemoveMessage

from app.infra_chat_agent.chat_state import ChatState
from app.infra_chat_agent.utils.graph_utils import get_tenant_id
from app.infra_chat_agent.config.tools_enum.reference_enums import normalize_geo_loc_code
from app.core.config import settings

logger = logging.getLogger(__name__)
_UUID_PATTERN = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)


def _normalize_create_resource(resource: str) -> str:
    """Normalize short CREATE resource aliases to canonical infra type codes."""
    normalized = (resource or "").strip().lower()
    alias_map = {
        "s3": "s3_infrastructuretype_ref",
        "sqs": "sqs_infrastructuretype_ref",
        "dynamodb": "dynamodb_infrastructuretype_ref",
        "database": "database_infrastructuretype_ref",
        "database_user": "database_user_infrastructuretype_ref",
        "kong_gateway": "kong_gateway_infrastructuretype_ref",
        "kong_route": "kong_gateway_infrastructuretype_ref",
    }
    return alias_map.get(normalized, resource or "")

# Resource-specific prompt sections injected dynamically based on running_resource
RESOURCE_SPECIFIC_SECTIONS = {
    "database_infrastructuretype_ref": {
        "priority_constraints": (
            '🚨 **TOP-LEVEL PRIORITY CONSTRAINTS** 🚨\n'
            '- **SINGLE SERVER**: You MUST support exactly ONE server per operation. '
            'Never attempt to create multiple databases in one flow.'
        ),
        "validation_rules": (
            '📐 **Validation & Formatting Rules**\n\n'
            '- Validate `environment` strictly — must be one of: `prod`, `stage`, `qa`.\n'
            '- Validate `geo_loc_code` and `product_name` using available lists or explicit user input. Never invent values.\n'
            '- When showing lists (servers, databases, etc), format them clearly:\n'
            '  Example:\n'
            '    1. db-server-001\n'
            '    2. db-server-002\n'
            '    _"Please select a server by number or name."_\n\n'
            '- When required parameters are missing, reply with:\n'
            '  _"Missing required parameters: [param1, param2]. Please provide them."_'
        ),
        "flow_instructions": (
            '📦 **Database Creation Flow (database_infrastructuretype_ref)**\n\n'
            'You MUST follow this sequence:\n'
            '1. Call `list_database_servers` to show available database servers.\n'
            '2. Present servers to user, and ask for selection.\n'
            '3. After user selects server, ask for remaining required parameters.\n'
            '4. Then call `create_database`.\n\n'
            'NEVER ask for `db_server_name` before showing server list.'
        ),
    },
    "sqs_infrastructuretype_ref": {
        "priority_constraints": "",
        "validation_rules": (
            '📐 **Validation & Formatting Rules**\n\n'
            '- The service validates environment, region, queue naming (SQS conventions), '
            'FIFO/DLQ settings, optional integer parameters (ranges), and cross-account IDs.\n'
            '- If a tool returns an error, explain it clearly to the user and ask for corrections.\n'
            '- When showing lists, format them clearly.'
        ),
        "flow_instructions": (
            '📦 **SQS Queue Creation Flow (sqs_infrastructuretype_ref)**\n\n'
            'IMPORTANT: The `create_sqs_queue` tool only collects and validates parameters — it does NOT actually create the queue.\n'
            'The user can update parameters later via the UI before pressing "Create PR" or "Deploy".\n'
            'Do NOT ask the user for confirmation — just call the tool once you have all required parameters.\n\n'
            'You MUST follow these rules:\n'
            '1. Call the tool with whatever parameters you have (from placement context + user message). '
            'The tool validates everything and returns structured errors for ALL missing or invalid parameters at once.\n'
            '2. Whenever you ask the user for missing required parameters — whether from a tool error response '
            'OR because you already know what is missing (e.g., the queue name) — do it in a SINGLE message. '
            'Present missing placement parameters FIRST, then tool-specific required parameters. '
            'If the tool returned valid options, include them so the user can pick.\n'
            '   After listing the required parameters, you MUST also list the optional attribute parameters from the tool schema '
            'that the user has NOT already provided. Show them as bulleted lines using <&h><&b>Label</&b> format. '
            'EXCLUDE any parameters listed in the "Placement Context" section above and the resource identifier — '
            'only show the remaining optional attribute parameters. '
            'For each, use the description from the tool schema to show its default or valid range. '
            'Only use information from the tool schema — do NOT invent values. '
            'These are informational — the user can optionally provide them but they are not required. '
            'Do NOT actively ask for them, just list them so the user knows they exist.\n'
            '   Do NOT show optional parameters in a success/ready response — only when asking for missing required ones.\n'
            '   NEVER ask for parameters one at a time — always ask for all missing ones together.\n'
            '3. When the user responds with the missing values, re-call the tool with ALL parameters '
            '(the ones you already had from context + the new ones from the user). '
            'Do NOT drop previously valid parameters.\n'
            '4. Optional parameters have defaults and are omitted from tool calls unless the user specifies them. '
            'If the user provides a value for any optional parameter, include it in the tool call.\n'
            '5. When the user provides a placement value by label (e.g., "Mumbai"), pass it directly to the tool — '
            'the tool handles label-to-value mapping internally.\n'
            '6. Use "region" instead of "geo_loc_code" when referring to geographic location in user-facing messages.\n'
            '7. NEVER show placement parameters in your responses to the user. '
            'Placement parameters are those listed in the "Placement Context" section above — they are internal context.'
        ),
    },
    "s3_infrastructuretype_ref": {
        "priority_constraints": "",
        "validation_rules": (
            '📐 **Validation & Formatting Rules**\n\n'
            '- The service validates environment, region, bucket naming (S3 conventions), '
            'replication rules, and duplicate buckets.\n'
            '- If a tool returns an error, explain it clearly to the user and ask for corrections.\n'
            '- When showing lists (buckets, etc), format them clearly.'
        ),
        "flow_instructions": (
            '📦 **S3 Bucket Creation Flow (s3_infrastructuretype_ref)**\n\n'
            'IMPORTANT: The `create_s3_bucket` tool only collects and validates parameters — it does NOT actually create the bucket.\n'
            'The user can update parameters later via the UI before pressing "Create PR" or "Deploy".\n'
            'Do NOT ask the user for confirmation — just call the tool once you have all required parameters.\n\n'
            'You MUST follow these rules:\n'
            '1. Call the tool with whatever parameters you have (from placement context + user message). '
            'The tool validates everything and returns structured errors for ALL missing or invalid parameters at once.\n'
            '2. Whenever you ask the user for missing required parameters — whether from a tool error response '
            'OR because you already know what is missing (e.g., the bucket name) — do it in a SINGLE message. '
            'Present missing placement parameters FIRST, then tool-specific required parameters. '
            'If the tool returned valid options, include them so the user can pick.\n'
            '   After listing the required parameters, you MUST also list the optional attribute parameters from the tool schema '
            'that the user has NOT already provided. Show them as bulleted lines using <&h><&b>Label</&b> format. '
            'EXCLUDE any parameters listed in the "Placement Context" section above and the resource identifier — '
            'only show the remaining optional attribute parameters. '
            'For each, use the description from the tool schema to show its default or valid range. '
            'Only use information from the tool schema — do NOT invent values. '
            'These are informational — the user can optionally provide them but they are not required. '
            'Do NOT actively ask for them, just list them so the user knows they exist.\n'
            '   Do NOT show optional parameters in a success/ready response — only when asking for missing required ones.\n'
            '   NEVER ask for parameters one at a time — always ask for all missing ones together.\n'
            '3. When the user responds with the missing values, re-call the tool with ALL parameters '
            '(the ones you already had from context + the new ones from the user). '
            'Do NOT drop previously valid parameters.\n'
            '4. Optional parameters have defaults and are omitted from tool calls unless the user specifies them. '
            'If the user provides a value for any optional parameter, include it in the tool call.\n'
            '5. `cross_account_account_id` is conditionally required: when `enable_s3_replication=true`, this field MUST be provided and must be exactly 12 digits.\n'
            '   If replication is false or omitted, `cross_account_account_id` remains optional.\n'
            '6. When the user provides a placement value by label (e.g., "Mumbai"), pass it directly to the tool — '
            'the tool handles label-to-value mapping internally.\n'
            '7. Use "region" instead of "geo_loc_code" when referring to geographic location in user-facing messages.'
        ),
    },
    "dynamodb_infrastructuretype_ref": {
        "priority_constraints": "",
        "validation_rules": (
            '📐 **Validation & Formatting Rules**\n\n'
            '- Do NOT validate parameter values yourself. Always pass user values directly to the tool — the service handles all validation.\n'
            '- If a tool returns an error, explain it clearly to the user and ask for corrections.'
        ),
        "flow_instructions": (
            '📦 **DynamoDB Table Creation Flow (dynamodb_infrastructuretype_ref)**\n\n'
            'IMPORTANT: The `create_dynamodb_table` tool only collects and validates parameters — it does NOT actually create the table.\n'
            'Parameters can be reviewed and updated later before final deployment.\n\n'
            '🚨 NEVER ask the user for confirmation, verification, or "is this correct?" prompts.\n'
            'Once you have all required parameters, call the tool IMMEDIATELY. Do NOT echo values back and wait for approval.\n\n'
            'You MUST follow this sequence:\n'
            '1. Ask the user for the following required parameters in a single message using the mandatory output format:\n'
            '   "Please provide the following details for the DynamoDB table:\n'
            '   <&h><&b>Table Name</&b> (identifier)\n'
            '   <&h><&b>Partition Key</&b> (attribute name, e.g., user_id)\n'
            '   <&h><&b>Partition Key Type</&b> (S = String, N = Number, B = Binary)\n\n'
            '   Tip: You can provide all values in one message, e.g. `Table Name: users, Partition Key: user_id, Partition Key Type: S`"\n'
            '2. When the user provides values, map them to parameters.\n'
            '   - If some required parameters are still missing: ask for the missing ones, then ALWAYS include a "Parameters collected so far:" section listing each received parameter and its value as bullet points.\n'
            '   - If ALL required parameters are available: call `create_dynamodb_table` IMMEDIATELY.\n'
            '   - NEVER validate, reject, correct, or re-ask about any value — even if it does not match the examples in step 1. Those examples are for user guidance only. Pass ALL user values to the tool EXACTLY as typed. The service is the ONLY validator.\n'
            '   - On error: state ONLY what is wrong — no extra sentences or suggestions. Reuse valid parameters; accept new values if the user provides them. Re-call the tool immediately after correction.\n'
            '3. After a successful tool call, provide a clear summary of the DynamoDB table configuration using the mandatory output format. Include a short confirmation line followed by the validated parameters as bullet points. Do NOT add extra sentences like "ready for further actions" or "you can proceed".\n'
            '4. NEVER show placement parameters (tenant_code, product_name, environment, geo_loc_code) in your responses to the user. These are internal context — the user already knows them.'
        ),
    },
}

# CREATE_FLOW_V2_SYSTEM_PROMPT = """You are a resource creation assistant for tenant: {tenant_code}

# You have access to MCP (Model Context Protocol) tools that can create infrastructure resources.
# These tools are designed to handle the actual creation of resources after gathering necessary parameters.

# **Available Tools:**
# {tools_description}

# **Your Responsibilities:**
# 1. Understand what the user wants to create
# 2. If there's a "list_*" tool available for the resource type, call it FIRST to show available options
# 3. Present the options to the user and let them choose
# 4. After selection, collect ALL remaining required parameters
# 5. Once ALL required parameters are collected, call the creation MCP tool
# 6. After tool execution, summarize the result for the user

# **CRITICAL: Two-Step Pattern for Resources with Options**

# Database Creation (database_infrastructuretype_ref):
# - For creating databases: FIRST call list_database_servers, THEN create_database
# - Always show users what options are available before asking them to choose
# - Don't ask for values like "db_server_name" until you've shown the available servers

# Database User Management (database_user_infrastructuretype_ref):
# - For managing database users: FIRST call list_database_servers, THEN list_databases_for_server, THEN structure_mysql_database_user_grants OR structure_psql_database_user_grants, THEN finalize_database_user_creation
# - CRITICAL: You MUST call structure_mysql_database_user_grants/structure_psql_database_user_grants BEFORE finalize_database_user_creation
# - The structure tool returns a 'structured_grants' object - you MUST extract this entire object and pass it as the 'structured_grants' parameter to finalize_database_user_creation
# - Example flow: structure_mysql_database_user_grants(...) returns {{"structured_grants": {{...}}, "is_ready": false}} -> then call finalize_database_user_creation(..., structured_grants={{...}})
# - Do NOT skip the structure step - finalize will fail without structured_grants from the structure tool
# - Show both servers AND databases on the selected server before asking for grant details
# - Use structure_mysql_database_user_grants for MySQL servers, structure_psql_database_user_grants for PostgreSQL servers
# - ALWAYS call finalize_database_user_creation after structuring grants to complete the flow
# - IMPORTANT: If user says "all databases" or "all db", you MUST create separate grant entries for EACH database listed by list_databases_for_server
# - For PostgreSQL with "all databases": Call structure_psql_database_user_grants ONCE per database (since pg_database is a single string, not an array)
# - ASK user: "Which databases do you want to grant access to?" and "Which grant levels do you want? (database-level, schema-level, table-level)"

# **MySQL Grants Format (One-level):**
# - mysql_databases: [{{"database": "db_name", "tables": "*", "privileges": ["SELECT", "INSERT", "UPDATE"]}}]
# - Tables can be specific names or "*" for all tables
# - Privileges: SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, DROP

# **PostgreSQL Grants Format:**
# - pg_database: Database name (e.g., "analytics")
# - pg_schema_grants: Array of schema/table grants
# - Three grant levels - all optional and independent:
#   - Database-level: CONNECT, CREATE
#   - Schema-level: USAGE, CREATE (object_type='schema')
#   - Table-level: SELECT, INSERT, UPDATE, DELETE (object_type='table')
# - User can grant at any level(s) independently - no dependencies between levels
# - Backend does NOT auto-add any grants
# - Ask user which grant levels they want and what privileges

# **CRITICAL: Check tool requirements carefully**
# - Each tool has specific required parameters listed above
# - You MUST collect ALL required parameters before calling the tool
# - Do NOT make assumptions about optional vs required - check the tool schema
# - When asking for missing parameters, list ALL that are missing

# **Common Parameters:**
# - tenant_code: Always use "{tenant_code}"
# - product_name: Product name (e.g., 'Core', 'Falcon')
# - environment: Environment (prod, stage, qa)
# - geo_loc_code: Geographic location (e.g., 'mumbai', 'singapore', 'region-aspora-mumbai')
# - Other parameters: Varies by tool (see tool descriptions above)

# **Interaction Guidelines:**
# - Be conversational and helpful
# - If a tool exists to list options, always call it first
# - Present options clearly and let users choose
# - If the user provides incomplete information, ask specifically for ALL missing required parameters
# - If a tool call fails, explain the error clearly and suggest how to fix it
# - After successful creation, provide a summary of what was created
# - Keep track of context within the conversation
# - CRITICAL for database users: ALWAYS ask user to specify which databases AND which grant levels they want - NEVER auto-select databases or grants

# **Important:**
# - Always use tenant_code: {tenant_code} when calling tools
# - Validate parameters before calling tools (e.g., environment must be one of: prod, stage, qa)
# - If a tool returns an error, don't retry automatically - explain and ask for corrections
# - After successful creation, provide a clear summary
# """

CREATE_FLOW_V2_SYSTEM_PROMPT = """You are a helpful assistant. NEVER guess or make up values for tool parameters.
Use the available tools for validation and submission. Each tool's description explains when and how to use it.

STEP 1 - MATCH PARAMETERS:
- Identify which tool the user wants (e.g. "s3" → CreateS3, "sqs" → CreateSQS, "kong"/"route" → CreateKongRoute).
- IGNORE filler words ("need", "create", "in", "with", "please") and the service name.
- Match each remaining value to a parameter:
  1. EXACT ENUM MATCH first.
  2. TYPE MATCH (e.g. "enabled"/"disabled"/"true"/"false" for boolean).
  3. FREE-TEXT FALLBACK for string params without enum.
- Each value → exactly ONE parameter.
- For boolean params: if user says "enable X and Y" or "X and Y enabled", apply the boolean value to BOTH params.
  E.g. "enable version and replication" → "version": "enable", "replication": "enable"
- Don't worry about exact spelling of param names. Pass the closest match — the server handles fuzzy matching.
  E.g. "replicat" → use "replicat" as the key, server will match it to "replication".
- Only extract values from the CURRENT message. Conversation state is managed by the system.
- If the user provides NO parameter values, pass an empty JSON object "{{}}".

STEP 2 - CORRECT AND FILTER MATCHED VALUES:
- For each matched value from STEP 1, check if the tool description lists allowed options or available values for that parameter.
- If a parameter has listed options, try to CORRECT the user's value to the closest option:
  - "londo" → "london", "flacon" → "falcon", "mmbai" → "mumbai", "londn" → "london", "cor" → "core"
  - Any typo, letter swap, missing letter, extra letter, or abbreviation → correct it and pass the corrected value.
  - DEFAULT to passing the value. Only reject if the value is completely unrelated to ALL available options (e.g. "banana" when options are [london, mumbai, canada]).
  - When rejecting, note it as a pre-validation failure with the available options.
- If a parameter has no listed options (free-text), pass it through as-is.
- Also verify value combinations: if the tool description shows which values are valid together, check that matched values are compatible with previously accepted values. Exclude incompatible values.

STEP 3 - CALL ValidateParams:
- You MUST call ValidateParams for EVERY user message. NEVER skip the tool call.
- Pass ONLY the values that passed STEP 2 pre-validation.
- If ALL values failed pre-validation, still call ValidateParams with an empty params "{{}}" so state is maintained.
- ALWAYS call ValidateParams with:
  - tool_name: the target tool (e.g. "CreateS3")
  - params: JSON string of pre-validated values from THIS message only
  - user_message: the user's raw message exactly as typed
- `tenant_id` is injected automatically by orchestration. Do NOT generate or change it.
- `conversation_state` is injected automatically by orchestration. Do NOT try to build it manually.
- `thread_id` may be present for tracing only.

STEP 4 - SHOW RESPONSE:
Combine any pre-validation failures from STEP 2 with the ValidateParams response into a single summary.

If the response has "is_ready": true → show the "message" field to the user. Done.

Otherwise respond in EXACTLY this format. No section headers, no extra text:

1. A direct response to what the user just said. Address their specific values:
   - For valid values: confirm them. E.g. "name 'myapp' and region 'us' look good."
   - For corrected typos: acknowledge briefly. E.g. "Auto-corrected 'enbale' → 'enable' for version."
   - For invalid values (from ValidateParams OR pre-validation): tell them what's wrong and show available options.
     E.g. "'rfe' is not a valid version — allowed: v1, v2."
   - For hallucinated values: flag them. E.g. "I didn't find 'xyz' in your message."

2. Then say "Here's what we have so far:" and loop through the "valid" array from index 0 to end:
  - If item has "default": true → print: ✓ param: value (default)
  - Otherwise → print: ✓ param: value
  Example:
  Here's what we have so far:
  ✓ name: myapp
  ✓ version: false (default)
  ✓ region: us

3. Then say "To create [label], we still need:" where [label] is the display value shown next to the tool name in the tool description (e.g. "a Kong Gateway route", "an S3 bucket"). Loop through the "missing" array from index 0 to end:
  - If item has "required": true → print: ○ param: hint
  - If item has "required": false → print: ○ param (optional): hint
  Example:
  To create an S3 bucket, we still need:
  ○ product: Product type. Allowed options: ['core', 'falcon', 'platform']
  ○ crossAccountId (optional): Cross account ID, required when replication is enabled

CRITICAL: Print valid and missing entries in array index order (0, 1, 2...). NEVER rearrange, sort, or skip entries. The order matters.
Do NOT include labels like "Part 1", "Part 2", "Part 3". No extra commentary or questions outside these sections."""



CREATE_FLOW_V2_SYSTEM_PROMPT_V2  = """You are a helpful assistant for tenant: {tenant_code}. NEVER guess or make up values for tool parameters.

MULTI-TURN ACCUMULATION:
- This is a multi-turn conversation. The user may provide parameters across multiple messages.
- ALWAYS carry forward ALL previously collected valid values from earlier turns.
- When showing "Collected Values", include BOTH values from the current message AND all valid values collected in previous turns.
- Only replace a previously collected value if the user explicitly provides a new value for the same parameter.

STEP 1 - PARAMETER MATCHING:
- First, IGNORE filler words and tool/service identifiers (e.g. "need", "create", "in", "with", "please", and the service name itself). These are NOT parameter values.
- For each remaining value, match it to a parameter using the tool schema in this priority order:
  1. EXACT ENUM MATCH: If the value exactly matches an enum option of a parameter → assign to that parameter.
  2. PATTERN MATCH: If the value resembles the format/pattern of a parameter's enum values (e.g. same prefix) but is not an exact match → assign to that parameter (it will be caught as invalid in STEP 2).
  3. TYPE MATCH: If the value matches a parameter's type (e.g. "true"/"false" for a boolean parameter) → assign to that parameter.
  4. FREE-TEXT FALLBACK: Only if no enum or type match is found → assign to a string parameter that has NO enum constraint.
- Each value is assigned to exactly ONE parameter. Do NOT assign the same value to multiple parameters.

STEP 2 - VALIDATION (do this AFTER matching):
- For EVERY matched parameter, validate the value against ALL constraints in the tool schema:
  (a) ENUM CHECK: If the parameter has an "enum" list, the value MUST exactly match one of the allowed options. If not → INVALID.
  (b) LENGTH CHECK: If the parameter description specifies length constraints (e.g. "min length = 3 and max lenght = 8"), the value MUST satisfy them. Otherwise it is INVALID.
  (c) CHARACTER CHECK: If the parameter description specifies character constraints (e.g. "letters only"), the value MUST satisfy them. Otherwise it is INVALID.
  (d) TYPE CHECK: The value must match the parameter's type (e.g. boolean must be true/false).
- INVALID values MUST go in "Missing Parameters" with the reason (e.g. "allowed options: ..." for enum, constraint from description for length/character violations, "must be true/false" for type violations).
- INVALID values MUST NOT appear in "Collected Values". This is critical.
- If a parameter has no value at all, it is MISSING. But ONLY check the "required" parameters — optional parameters that are not provided are NOT missing.
- NEVER silently substitute, guess, or ignore any value.
- If the tool description states that a parameter is conditionally required based on another parameter's value, enforce that rule during parameter collection.

STEP 3 - ACTION:
- If there are ANY missing or invalid REQUIRED parameters: show the OUTPUT FORMAT below. Do NOT call the tool.
- If ALL required parameters have valid values: IMMEDIATELY call the tool. Do NOT show "Collected Values" — just call the tool directly.

CATEGORIZATION RULES (apply these strictly):
- A value is INVALID if it fails ANY validation: enum mismatch, format violation, or constraint violation.
- "Collected Values" → ONLY parameters whose value passed ALL validations (enum, format, constraints). Show as: key: value
- "Missing Parameters" → required parameters that are either:
  (a) INVALID — value provided but failed validation → show as: key: You provided "X" — [reason]
  (b) MISSING — no value provided at all → show as: key: Missing — [what to provide]
- A parameter MUST appear in only ONE section. If INVALID, it goes in "Missing Parameters", NEVER in "Collected Values".

OUTPUT FORMAT (ONLY use when there are missing/invalid required parameters):
- "Collected Values": ONLY valid values. NEVER put invalid values or error messages here.
- "Missing Parameters": You MUST iterate over EVERY required parameter in the tool schema. If a required parameter has no valid value yet, it MUST appear here. Do NOT list optional parameters here.

IMPORTANT: The examples below are illustrative only. Always check ALL required parameters from the tool schema — do NOT limit yourself to only the parameters shown in the examples.

EXAMPLE 1 — MISSING PARAMS — user says "need s3 myname uk v9":
  Collected Values:
  - <&h><&b>name</&b>: myname

  Missing Parameters:
  - <&h><&b>version</&b>: You provided "v9" — allowed options: "v1", "v2"
  - <&h><&b>region</&b>: You provided "uk" — allowed options: "us", "mumbai"
  - ... (plus any other required parameters from the tool schema that have no valid value)

  WRONG output (DO NOT do this):
  Collected Values:
  - name: myname
  - version: v9     ← WRONG! v9 is not a valid option, must be in Missing Parameters
  - region: uk      ← WRONG! uk is not a valid option, must be in Missing Parameters

EXAMPLE 2 — ALL VALID — user says "need s3 myname v2 us core false":
  All required parameters are valid. Do NOT show Collected Values. CALL the CreateS3 tool immediately.
"""
CREATE_FLOW_V2_SYSTEM_PROMPT_V1 = """
You are a resource creation assistant for tenant: {tenant_code}

{priority_constraints}

=== MANDATORY OUTPUT FORMAT ===
BULLET POINTS: Put "<&h>" at the START of each line (it's a prefix, NOT an HTML tag, no closing tag needed)
BOLD TEXT: Wrap text with "<&b>" and "</&b>"

CORRECT:
<&h><&b>Server</&b>: mysql-1
<&h><&b>Database</&b>: mydb

WRONG (never do this):
• Server: value
- Server: value
**Server**: value
===============================

You interface with MCP (Model Context Protocol) tools to orchestrate the creation and configuration of infrastructure resources.

---

🔧 **Available Tools**
{tools_description}

---

🎯 **Primary Objective**
Use MCP tools to create infrastructure resources by guiding the user through a reliable, step-by-step process:
- Ask for all required parameters (no guessing)
- Use available "list_*" tools first to show options before asking the user to select
- Call the creation tool once the user has provided values for all required parameters — do not validate the values, just pass them through

---

⚙️ **Strict Tool Usage Rules**

1. **DO NOT fabricate values or assume defaults.**
   - Never invent parameters, server names, environments, or grant types.
   - Only use values explicitly provided by the user or listed via MCP tools.

2. **DO NOT skip required steps.**
   - Always call "list_*" tools before asking the user to choose from options.
   - Ensure the user has provided values for all required parameters before calling the tool. Do NOT validate the values yourself — always pass them to the tool and let the service validate.

3. **DO NOT infer intentions.**
   - If the user hasn't specified what resource to create, ask specifically. But if the user has provided parameter values, always pass them to the tool — even if they look incorrect.

---

{validation_rules}

---

📊 **Standard Parameters (always required):**
- tenant_code: Use "{tenant_code}"
- product_name: e.g., 'Core', 'Falcon'
- environment: Must be one of ['prod', 'stage', 'qa']
- geo_loc_code: e.g., 'region-aspora-mumbai', 'region-aspora-london'

🧾 **Placement Context (already collected)**
{placement_context}

---

{flow_instructions}

---

🧠 Behavior Guidelines
- Be conversational and friendly — use natural, human-readable language. Avoid robotic or templated responses.
- Track conversation state: store all confirmed selections for re-use.
- Only proceed to the next step when the current one is complete.
- If a tool call fails, explain the exact error and how to fix it.
- **Error recovery**: When a tool call fails with a validation error, ask the user to correct ONLY the invalid parameter(s). When the user responds, use any new values they provide (even for previously valid parameters). For parameters the user does not mention, reuse the values from the previous attempt. Then re-call the tool immediately. Do NOT ask the user to re-enter all parameters.
- After successful creation, provide a clear, human-readable summary of what was created.
- **Parameter updates after success**: If the user wants to change ANY parameter after a successful tool response (e.g., change environment, change product), you MUST re-call the tool with ALL parameters (previous values + the updated ones). NEVER just acknowledge the change in text — the tool must be called again so the updated parameters are captured. This applies to both attribute parameters and placement parameters.

⚠️ Final Notes
- Never make up server names, parameter values, or grant structures.
- Always use tenant_code: {tenant_code}
- Do not retry failed tool calls automatically. Ask the user for corrected input.
"""


async def _resolve_product_name_for_prompt(placement_params: dict) -> str | None:
    """
    Resolve placement product value to a display name for prompt/context.

    Priority:
    1. _applications_mst_name
    2. product_name
    3. applications_mst_code (resolved from DB if UUID)
    """
    resolved_product_name = (
        placement_params.get("_applications_mst_name")
        or placement_params.get("product_name")
    )
    if isinstance(resolved_product_name, str):
        normalized = resolved_product_name.strip()
        if normalized and normalized.lower() not in {"none", "null"}:
            return normalized

    applications_mst_code = placement_params.get("applications_mst_code")
    if not isinstance(applications_mst_code, str):
        return resolved_product_name

    applications_mst_code = applications_mst_code.strip()
    if not applications_mst_code or applications_mst_code.lower() in {"none", "null"}:
        return resolved_product_name

    # Non-UUID values are usually already product labels/codes.
    if not _UUID_PATTERN.match(applications_mst_code):
        return applications_mst_code

    try:
        from app.db.session import AsyncSessionLocal
        from app.repository.applications_mst_repository import ApplicationsMstRepository

        async with AsyncSessionLocal() as db:
            app_repo = ApplicationsMstRepository(db)
            app_record = await app_repo.get_by_code(applications_mst_code)
            if app_record and app_record.name:
                return app_record.name
    except Exception as exc:
        logger.warning(
            "[CREATE_FLOW_V2] Failed to resolve applications_mst_code to product name: "
            f"applications_mst_code={applications_mst_code!r}, error={exc}"
        )

    # Fallback to raw code if resolution fails.
    return applications_mst_code

def create_flow_v2_node(resource_tools_map: dict):
    """
    Create flow v2 node function with per-resource MCP tools.

    This node is designed to be extensible - as new MCP tools are added,
    they will automatically be available to the LLM without code changes.

    The node follows the tool loop pattern:
    1. LLM with bound tools decides which tool to call
    2. If tool calls: route to tool_node for execution, then loop back here
    3. If no tool calls: LLM provided final response, route to response_handler

    Args:
        resource_tools_map: Dict mapping resource type to filtered MCP tools,
                           e.g. {"database_infrastructuretype_ref": [list_db_servers, create_db], ...}

    Returns:
        Async node function
    """
    _llm_cache = {}         # resource_type → LLM (normal, tool_choice=auto)
    _llm_cache_forced = {}  # resource_type → LLM (tool_choice=ValidateParams)

    def get_llm_with_tools(resource_type: str, force_validate: bool = False):
        """Lazy initialization of LLM with tools, cached per resource type.

        force_validate=True: first entry of each turn — LLM must call ValidateParams.
        force_validate=False: subsequent entries — LLM decides freely.
        """
        cache = _llm_cache_forced if force_validate else _llm_cache
        if resource_type not in cache:
            tools = resource_tools_map.get(resource_type, [])
            llm = ChatOpenAI(
                api_key=settings.openai_api_key,
                model="gpt-4o",
                temperature=0
            )
            kwargs = (
                {"tool_choice": {"type": "function", "function": {"name": "ValidateParams"}}}
                if force_validate else {}
            )
            cache[resource_type] = llm.bind_tools(tools, **kwargs)
        return cache[resource_type]

    async def create_flow_v2(state: ChatState, config) -> dict:
        """
        Invoke LLM with MCP tools to handle CREATE intent.

        This is the v2 CREATE flow that uses MCP tools for actual resource creation.
        It replaces the traditional parameter extraction → policy validation flow
        with a more flexible LLM + tools approach.

        Flow:
        1. Build system prompt with available tools
        2. Use messages state for tool loops (within-request)
        3. LLM decides: call tool OR ask for more information
        4. If tool calls: graph routes to tool_node, then loops back here
        5. If no tool calls: LLM provided final response, route to response_handler

        The messages state maintains conversation history within this request:
        - First call: [SystemMessage, HumanMessage] → LLM with tool_calls
        - Second call (after tool execution): [..., AIMessage with tool_calls, ToolMessage] → LLM final response

        Args:
            state: Current chat state
            config: Graph configuration (contains thread_id)

        Returns:
            Dict with updated messages
        """
        tenant_id = get_tenant_id(state)
        thread_id = config.get("configurable", {}).get("thread_id", "")
        user_message = state.get("user_message", "").strip()
        placement_params = state.get("collected_placement_parameters") or {}

        resolved_environment = placement_params.get("environment_enum") or placement_params.get("environment")
        if isinstance(resolved_environment, str):
            resolved_environment = resolved_environment.lower().strip()

        resolved_product_name = await _resolve_product_name_for_prompt(placement_params)

        resolved_geo_loc = normalize_geo_loc_code(
            placement_params.get("geo_loc_mst_code") or placement_params.get("geo_loc_code") or ""
        )

        resolved_placement = {
            "tenant_code": placement_params.get("tenant_code") or tenant_id,
            "product_name": resolved_product_name,
            "environment": resolved_environment,
            "geo_loc_code": resolved_geo_loc,
        }

        placement_signature = "|".join(
            str(value).strip().lower() if value is not None else ""
            for value in (
                resolved_placement["tenant_code"],
                resolved_placement["product_name"],
                resolved_placement["environment"],
                resolved_placement["geo_loc_code"],
            )
        )

        # Get existing messages from state (for tool loop continuation)
        messages: List[BaseMessage] = list(state.get("messages") or [])

        last_signature = state.get("create_flow_v2_prompt_signature")
        if messages and user_message and last_signature != placement_signature:
            placement_update_context = (
                "Placement updated: "
                f"tenant={resolved_placement['tenant_code']}, "
                f"product={resolved_placement['product_name']}, "
                f"env={resolved_placement['environment']}, "
                f"geo={resolved_placement['geo_loc_code']}\n"
                "Use these values for tool calls."
            )
            messages.append(SystemMessage(content=placement_update_context))
            logger.info(
                "[CREATE_FLOW_V2] Appended placement update system message: "
                f"old_signature={last_signature}, new_signature={placement_signature}"
            )

        # Determine resource type for resource-specific tool selection
        state_hint = state.get("state_hint") or {}
        running_resource = state_hint.get("running_resource") or state.get("turn_resource", "")
        running_resource = _normalize_create_resource(running_resource)

        # First invocation: build fresh context
        if not messages:
            resource_tools = resource_tools_map.get(running_resource, [])

            # NOTE: The following code built tools_description, placement_context,
            # and resource_sections for the old V1 prompt. The active
            # CREATE_FLOW_V2_SYSTEM_PROMPT is concise and does not enumerate tools inline.
            # Kept commented out for reference if switching back to V1 prompt.
            #
            # tool_descriptions = []
            # for tool in resource_tools:
            #     name = tool.name
            #     desc = tool.description if hasattr(tool, 'description') else "No description"
            #     tool_descriptions.append(f"- **{name}**: {desc}")
            # tools_desc = "\n".join(tool_descriptions)
            # resource_sections = RESOURCE_SPECIFIC_SECTIONS.get(running_resource, {})
            #
            # available_lines = []
            # missing_keys = []
            # for key, value in resolved_placement.items():
            #     if value:
            #         available_lines.append(f"- {key}: {value}")
            #     else:
            #         missing_keys.append(key)
            #
            # user_override_instruction = (
            #     "\n\nIMPORTANT — Placement priority: If the user explicitly mentions "
            #     "placement values in their message (e.g., a different environment, product, "
            #     "or region), ALWAYS use the user's values instead of the defaults above. "
            #     "For any placement parameter the user does NOT mention, use the default "
            #     "value listed above. Pass all values to the tool — it handles validation "
            #     "and resolution."
            # )
            #
            # if available_lines and not missing_keys:
            #     placement_context = (
            #         "Default placement values (from context):\n"
            #         + "\n".join(available_lines)
            #         + user_override_instruction
            #     )
            # elif available_lines:
            #     placement_context = (
            #         "Default placement values collected so far:\n"
            #         + "\n".join(available_lines)
            #         + f"\n\nMissing placement parameters: {missing_keys}. "
            #         "The user's message may contain these missing values — extract them "
            #         "and pass to the tool. The tool will return any still-missing parameters "
            #         "with valid options."
            #         + user_override_instruction
            #     )
            # else:
            #     placement_context = (
            #         "No default placement parameters available. "
            #         "Extract placement values from the user's message and pass them to "
            #         "the tool. The tool will return any missing parameters with valid options."
            #     )

            system_content = CREATE_FLOW_V2_SYSTEM_PROMPT.format(
                tenant_code=tenant_id,
                thread_id=thread_id,
            )
            messages = [SystemMessage(content=system_content)]

            logger.info(
                f"[CREATE_FLOW_V2] Initialized with {len(resource_tools)} tools "
                f"for resource={running_resource}, tenant={tenant_id}, thread_id={thread_id}"
            )

        # Sanitize messages: remove trailing AIMessage with orphaned tool_calls.
        # This can happen when a previous turn's tool call was short-circuited by
        # should_continue_tools_v2 (e.g., old is_ready=true detected), leaving an
        # AIMessage with tool_calls but no corresponding ToolMessage in the history.
        # We must use RemoveMessage to actually remove from the checkpoint — the
        # add_messages reducer is append-only and ignores deletions-by-omission.
        orphaned_ids = []
        while messages and isinstance(messages[-1], AIMessage) and getattr(messages[-1], "tool_calls", None):
            tool_call_ids = {tc["id"] for tc in messages[-1].tool_calls}
            has_responses = any(
                isinstance(m, ToolMessage) and getattr(m, "tool_call_id", None) in tool_call_ids
                for m in messages
            )
            if not has_responses:
                orphaned_msg = messages.pop()
                orphaned_ids.append(orphaned_msg.id)
                logger.warning(
                    f"[CREATE_FLOW_V2] Removing orphaned AIMessage with tool_calls "
                    f"(ids={tool_call_ids}, msg_id={orphaned_msg.id}) — no matching ToolMessage found"
                )
            else:
                break

        # Add user message only on fresh invocations, NOT on tool loop continuations.
        # Tool loop: LLM called a tool → tool_node executed → routed back here.
        #   The last message is a ToolMessage AND the user_message is already in
        #   the history (as a HumanMessage). Do NOT re-add it or the LLM will see
        #   a duplicate and call the tool again indefinitely.
        # New turn: User sent a new message after a previous turn completed.
        #   The last message may also be a ToolMessage (from previous turn's
        #   is_ready result), but the new user_message is NOT yet in the history.
        #   We MUST add it so the LLM can process the update request.
        user_message_already_in_history = any(
            isinstance(m, HumanMessage) and m.content == user_message
            for m in messages
        ) if user_message else False
        if user_message and not user_message_already_in_history:
            messages.append(HumanMessage(content=user_message))

        logger.info(
            f"[CREATE_FLOW_V2] Invoking LLM - "
            f"tenant={tenant_id}, messages={len(messages)}, "
            f"user_msg='{user_message[:100] if user_message else '(none)'}...'"
        )

        # Force ValidateParams on the first entry of each turn.
        # Detect: no ToolMessage exists after the most recent HumanMessage.
        is_first_entry = True
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                break
            if isinstance(msg, ToolMessage):
                is_first_entry = False
                break

        if is_first_entry:
            logger.info("[CREATE_FLOW_V2] First entry this turn — forcing ValidateParams call")

        # Invoke LLM with resource-specific tools
        response: AIMessage = await get_llm_with_tools(running_resource, force_validate=is_first_entry).ainvoke(messages)

        has_tool_calls = bool(getattr(response, "tool_calls", None))
        logger.info(
            f"[CREATE_FLOW_V2] LLM response - "
            f"has_tool_calls={has_tool_calls}, "
            f"content_len={len(response.content) if response.content else 0}"
        )

        # Return updated messages (includes response)
        # Graph routing logic (should_continue_tools_v2) will check for tool_calls
        # Include RemoveMessage objects so the add_messages reducer actually
        # deletes orphaned AIMessages from the checkpoint.
        remove_messages = [RemoveMessage(id=mid) for mid in orphaned_ids]
        return {
            "messages": remove_messages + messages + [response],
            "create_flow_v2_prompt_signature": placement_signature,
        }

    return create_flow_v2

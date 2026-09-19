"""
Intent detection prompt for Service Config Assistant.
"""

from app.utils.service_config_chat.prompts.parameter_definitions import (
    format_extraction_parameters,
    format_extraction_aliases,
)

INTENT_DETECTION_PROMPT = """Analyze the user's message and determine their intent.

Context: User is configuring a new service and looking for REFERENCE configs from OTHER services.

Current State:
{state_context}
{history_context}
User Message: "{user_message}"

Known parameter names and aliases (IMPORTANT - these are NOT service names):
cpu, memory, ram, port, health, healthcheck, health check, health_check_path,
cpu requested, cpu_requested, cpu limit, cpu_limit, memory requested, memory_requested, memory limit, memory_limit,
replica count, replica_count, min replicas, min_replicas, max replicas, max_replicas, cpu threshold, cpu_threshold, memory threshold, memory_threshold, hpa,
scaling, autoscaling, replicas, instances, desired count, min tasks, max tasks,
http scaling, target value, http target, scaling target,
alb, lb, priority, alb selection, listener, listener priority, listener rule, lister priority, rule,
datadog, dd, datadog cpu, datadog memory, log source, sidecar,
ebs, storage, disk, volume, ebs size, ebs type, ebs volume,
heap, jvm, xms, xmx,
dockerfile, dockerfile path, build, build path, wire, wire path,
repo, repository, github, git hub, branch, branches, trigger paths, trigger path, other paths,
ulimits, service path,
language, lang, programming language, framework, runtime, tech stack

Possible intents (respond with ONLY one of these):
- list_services: User wants to see available reference services or existing configs (e.g., "list", "list services", "show services", "what services", "services", "existing configs", "what configs exist", "available configs", "show references")
- select_service: User mentions a SPECIFIC service name they want to use as reference (e.g., "refer surya-2", "use payment-service config", "i want to see user-api"). If user mentions a service name (not a parameter), this is select_service.
- show_config: User wants to see config for ALREADY selected reference service (e.g., "show config", "what's the config"). Only use this if a reference service is already selected.
- form_fill: User wants to fill form with reference config (e.g., "yes", "fill form", "use this", "apply", "autofill", "fill it", "do it", "ok", "sure", "yes please"). Use this when user confirms after seeing a config.
- decline: User declines an offer or says no (e.g., "no", "nope", "no thanks", "not now", "skip", "nevermind", "cancel"). Use this when user declines form fill or other offers.
- ask_parameter: User is asking about a config parameter - either by name, with "?", or asking "what is X" where X is a parameter. Includes short queries like "alb?", "cpu?", "memory?", "ebs?", "scaling?". If the message contains a known parameter name/alias from the list above, this is likely ask_parameter.
- list_parameters: User wants to see what parameters they can ask about (e.g., "list parameters", "what parameters", "which parameters can you help with", "show parameters", "parameters", "help with parameters", "parameter help")
- help: User needs general guidance, greetings, or asks about capabilities (e.g., "help", "what can you do", "what do you do", "who are you", "what are you", "how does this work", "hi", "hello", "hey", "your capabilities", "what is this")
- out_of_scope: Anything else (e.g., "modify cpu", "create config", "new service", "new config", "compare environments")

IMPORTANT distinctions:
- "alb?" → ask_parameter (alb is a known parameter alias)
- "cpu?" → ask_parameter (cpu is a known parameter)
- "ebs?" → ask_parameter (ebs is a known parameter alias)
- "build path" → ask_parameter (asking about build_path parameter)
- "health check" → ask_parameter (asking about health_check_path)
- "language" → ask_parameter (language is a known parameter)
- "lang" → ask_parameter (lang is an alias for language)
- "what do you do" → help (asking about capabilities, NOT a parameter)
- "what is cpu" → ask_parameter (cpu is a specific parameter)
- "what is a sample value" → ask_parameter (will ask for clarification)
- "payment-service" → select_service (not a parameter name)
- "refer X config" or "i need X config" → select_service (X is a service name, not a parameter)
- "what are the existing configs" → list_services (asking what's available)
- "show me the reference" → list_services (if no reference selected yet)
- "available services" → list_services

Respond with ONLY the intent name, nothing else."""

SERVICE_EXTRACTION_PROMPT = """Extract the reference service name or search term from this message.

Message: "{user_message}"

If the user is clearly referring to a service, extract the name/term they used.
If no service name is mentioned, respond with "NONE".

Respond with ONLY the extracted service name/term or "NONE", nothing else."""


def build_parameter_extraction_prompt() -> str:
    """Build the parameter extraction prompt with all parameters."""
    return f"""Extract the config parameter name from this message.

Message: "{{user_message}}"

All known parameters:
{format_extraction_parameters()}

Common aliases:
{format_extraction_aliases()}

Rules:
1. Extract the parameter name the user is asking about
2. Strip punctuation like "?" from parameter names (e.g., "alb?" → alb)
3. If user says just "scaling" alone (ambiguous) → AMBIGUOUS_SCALING
4. "sidecar" → enable_datadog_sidecar (only Datadog sidecar is supported)
5. "enable hpa", "autoscale hpa", "hpa enabled" → enable_autoscaling (the boolean toggle)
6. If user says just "hpa" alone (ambiguous, involves multiple params) → AMBIGUOUS_HPA
7. If message is a general question with NO specific parameter → NONE
8. Words like "what", "is", "the", "param", "parameter", "setting" are NOT parameter names
9. "http scaling" or "request scaling" → http_scaling_enabled
10. "task scaling" or "autoscaling" → enable_autoscaling
11. "datadog" or "dd" → enable_datadog_sidecar

Examples:
- "alb?" → alb
- "cpu?" → cpu
- "ebs?" → ebs
- "what is cpu" → cpu
- "tell me about dockerfile" → dockerfile
- "what is the memory setting" → memory
- "what does xmx mean" → xmx
- "dockerfile param" → dockerfile
- "build path" → build path
- "health check" → health check
- "service path" → service path
- "desired count" → desired count
- "cpu requested" → cpu requested
- "cpu limit" → cpu limit
- "memory requested" → memory requested
- "memory limit" → memory limit
- "replica count" → replica count
- "min replicas" → min replicas
- "max replicas" → max replicas
- "cpu threshold" → cpu threshold
- "memory threshold" → memory threshold
- "what do you do" → NONE (no parameter mentioned)
- "what is a sample value" → NONE (no specific parameter)
- "hi" → NONE
- "help" → NONE

Respond with ONLY the parameter name, AMBIGUOUS_SCALING, AMBIGUOUS_HPA, or NONE."""


# Build the prompt at module load time
PARAMETER_EXTRACTION_PROMPT = build_parameter_extraction_prompt()


CUSTOM_VALUE_EXTRACTION_PROMPT = """Extract the value the user wants to set for a config parameter.

Field: {field}
Suggested value: {suggested_value}
User message: "{user_message}"

Rules:
1. If user provides a SPECIFIC value (number, boolean, string), extract it
2. If user just confirms (yes/ok/sure) → respond NONE
3. If user just declines (no/nope) → respond NONE
4. For booleans: "enable"/"true" → true, "disable"/"false" → false

Examples:
- "yes" → NONE
- "ok" → NONE
- "no" → NONE
- "7" → 7
- "use 7" → 7
- "no, fill with 1024" → 1024
- "set it to 512" → 512
- "true" → true
- "enable" → true
- "false" → false
- "disable it" → false

Respond with ONLY the extracted value or NONE. No explanation."""

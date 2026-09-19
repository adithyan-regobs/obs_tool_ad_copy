"""
Response generation prompts for Service Config Assistant.
"""

LIST_SERVICES_PROMPT = """Tell the user about available reference services.

There are {total} services with existing configs available above.
{infra_context}

Say: "Here are {total}{infra_suffix} services with configs. Select one to view its configuration."

Do NOT list the services in text - they are shown as clickable options above. Keep it to 1 sentence."""

LIST_SERVICES_EMPTY_PROMPT = """Tell the user no other services were found to use as reference.
Suggest checking if other services exist for this tenant."""

SELECT_SERVICE_FOUND_PROMPT = """Confirm the reference service selection.

Selected Reference Service: {service_name} ({service_code})
{config_note}

Ask if they'd like to see the configuration to use as reference."""

SELECT_SERVICE_MATCHES_PROMPT = """Present these possible matches to the user.

Possible matches:
{matches_list}

Ask user to confirm which reference service they meant."""

SELECT_SERVICE_NOT_FOUND_PROMPT = """Tell the user no matching reference services were found.
Ask them to try a different name or list all services."""

SHOW_CONFIG_PROMPT = """Show the reference configuration to the user.

Reference Service: {service_name}
{source_note}
Configuration:
{config_summary}

Keep it concise. End with: "Would you like to fill the form with this config?"
Do NOT mention JSON or any format options."""

SHOW_CONFIG_EMPTY_PROMPT = """Tell the user no configuration exists for {service_name} to use as reference.
Suggest trying another service."""

HELP_PROMPT = """Respond to the greeting or help request.

Say EXACTLY this (copy the format precisely):
"Hey. I am your service config assistant.

I can help with:

- Fill form from reference: list services -> view config -> autofill
- Config parameters:
    • Container: cpu, memory, port, ulimits
    • Health & Routing: health_check_path, service_path
    • Scaling: autoscaling, desired_count, min/max tasks
    • HTTP Scaling: http_scaling_enabled, target_value
    • ALB: alb_selection, listener_rule_priority
    • Sidecars: Datadog (enable, cpu, memory)
    • EBS: ebs_enabled, volume, size, type
    • JVM: xms, xmx
    • Build: build_path, dockerfile_path, wire
    • Repository: repository, branches, trigger_paths

Let me know."

IMPORTANT: Use dash (-) for main items, bullet (•) with 4 spaces indent for sub-items. Copy exactly."""

OUT_OF_SCOPE_PROMPT = """The user asked: "{user_message}"

This is outside your scope. Politely redirect.

Say EXACTLY this:
"I can't help with that.

I can help with:

- Fill form from reference: list services -> view config -> autofill
- Config parameters: cpu, memory, health, scaling, HTTP scaling, ALB, sidecars, EBS, JVM, build, repository

Let me know."

Use plain text list with dashes. No markdown."""


ASK_PARAMETER_PROMPT = """Answer the parameter question.

Parameter: {parameter_name}
Definition: {definition}
Environment: {environment}
{infra_context}

Stats in {environment}{infra_suffix}:
- Most common value: {most_common}
- Used by {total_services} services ({total_configs} configs)
{outlier_note}

Format your response as:
"{definition}

In {environment}{infra_suffix}:
- Most common: {most_common}
- {total_services} services use this"

If there's a notable outlier, add a bullet for it.
Keep it scannable. No markdown formatting."""


ASK_PARAMETER_NO_DATA_PROMPT = """Answer the parameter question with definition only.

Parameter: {parameter_name}
Definition: {definition}

No usage data from current configs.
Respond with: "{definition}. Not set in current configs."
Keep it under 1 sentence. No markdown."""


LIST_PARAMETERS_PROMPT = """List the parameter categories the user can ask about.

Say EXACTLY this:
"I can help with these parameters:

- Container: cpu, memory, port, ulimits
- Health & Routing: health_check_path, service_path
- Scaling: autoscaling, desired_count, min/max tasks
- HTTP Scaling: http_scaling_enabled, target_value
- ALB: alb_selection, listener_rule_priority
- Sidecars: Datadog (enable, cpu, memory)
- EBS: ebs_enabled, volume, size, type
- JVM: xms, xmx
- Build: build_path, dockerfile_path, wire
- Repository: repository, branches, trigger_paths

Let me know."

Use plain text list with dashes. No markdown formatting."""


FORM_FILL_PROMPT = """Confirm the config is being applied.

Reference Service: {service_name}

Say: "Filling form with {service_name} config. Review and adjust values as needed."
Keep it to 1 sentence."""

FORM_FILL_NO_SERVICE_PROMPT = """Tell user to select a reference service first.

Say: "Select a reference service first. List services or type a name to find one."
Keep it to 1 sentence."""

DECLINE_PROMPT = """User declined. Acknowledge briefly and offer alternatives.

Say: "No problem. Say 'list services' to pick another, or ask about a parameter."
Keep it to 1 sentence. No friction."""

CLARIFY_AFTER_DECLINE_PROMPT = """User said yes after declining. Clarify what they want.

Reference Service: {service_name}

Say: "Would you like to fill with {service_name} config, or list services to pick another?"
Keep it to 1 sentence."""

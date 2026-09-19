"""
Prompt builder for parameter collection and extraction.

This module provides metadata-driven prompt generation for parameters,
keeping prompts consistent and maintainable.
"""
from typing import List

from app.infra_chat_agent.config.config_models import (
    ResourceMeta,
    ParameterMeta,
    ParamType,
    StaticSource,
)


class DefaultPromptBuilder:
    """
    Default, metadata-driven prompt builder.

    This builder is:
    - Deterministic
    - Resource-agnostic
    - Tenant-agnostic
    - Safe for LLM use
    """

    @staticmethod
    def build_param_prompt(
        *,
        resource_meta: ResourceMeta,
        param: ParameterMeta,
    ) -> str:
        """
        Generate a prompt to collect ONE parameter.

        This produces a human-readable description that can be used:
        - Directly in chat responses (to ask user for value)
        - As context in extraction prompts (to help LLM understand what to extract)

        Args:
            resource_meta: Full resource metadata
            param: Parameter to generate prompt for

        Returns:
            Human-readable prompt text for this parameter
        """

        lines: List[str] = []

        # 1. Base question - use name (human-readable display name)
        display_name = str(param.name)

        if param.required:
            lines.append(f"Please provide **{display_name}**.")
        else:
            lines.append(f"Optionally, provide **{display_name}**.")

        # 2. Description hint
        if param.description:
            lines.append(param.description)

        # 3. Type-specific hints
        if param.type == ParamType.ENUM:
            lines.append("Choose one of the following options.")

        elif param.type == ParamType.BOOL:
            lines.append("Reply with `true` or `false`.")

        elif param.type == ParamType.INT:
            lines.append("Provide a numeric value.")

        # 4. Value source hints (safe, non-leaky)
        if isinstance(param.value_source, StaticSource):
            opts = [o.label for o in param.value_source.options]
            if opts:
                lines.append(f"Available options: {', '.join(opts)}")

        # DB / API sources - do NOT list values in prompt
        # (UI or Slack will render them separately)

        # 5. Default hint
        if param.default is not None:
            lines.append(f"Default: `{param.default}`")

        # 6. Validation hint
        if param.validation:
            if param.validation.allowed:
                lines.append(
                    f"Allowed values: {', '.join(param.validation.allowed)}"
                )
            if param.validation.regex:
                lines.append("Value must match the required format.")

        # 7. Examples
        if param.examples:
            lines.append("Examples:")
            for ex in param.examples:
                lines.append(f"- {ex}")

        return "\n".join(lines)

    @staticmethod
    def build_param_description(
        *,
        param: ParameterMeta,
        include_hints: bool = True,
    ) -> str:
        """
        Build a compact parameter description for extraction context.

        This is a shorter version of build_param_prompt() optimized
        for inclusion in extraction prompts where brevity matters.

        Args:
            param: Parameter to describe
            include_hints: Whether to include validation/value source hints

        Returns:
            Compact description string
        """
        parts: List[str] = []

        # Name and required - use display name
        required_marker = "REQUIRED" if param.required else "optional"
        parts.append(f"**{str(param.name)}** ({required_marker})")

        # Type
        parts.append(f"Type: {param.type.value}")

        # Description
        if param.description:
            parts.append(f"- {param.description}")

        # Hints
        if include_hints:
            # Value source hints
            if isinstance(param.value_source, StaticSource):
                opts = [o.label for o in param.value_source.options]
                if opts:
                    parts.append(f"Options: {', '.join(opts)}")

            # Validation
            if param.validation and param.validation.allowed:
                parts.append(f"Allowed: {', '.join(param.validation.allowed)}")

            # Default
            if param.default is not None:
                parts.append(f"Default: {param.default}")

        # Examples
        if param.examples:
            parts.append(f"Examples: {', '.join(param.examples)}")

        return " ".join(parts)

    @staticmethod
    def build_intent_detection_param_examples(
        *,
        param: ParameterMeta,
        resource_display_name: str,
        running_resource_code: str,
    ) -> List[str]:
        """
        Build intent detection examples for a parameter during ongoing CREATE workflows.

        Generates examples showing various ways users might provide this parameter value,
        helping the intent detector recognize parameter inputs as CREATE intent.

        Args:
            param: Parameter metadata
            resource_display_name: Resource display name (e.g., "s3", "sqs")
            running_resource_code: Database code (e.g., "s3_infrastructuretype_ref")

        Returns:
            List of example strings (formatted for intent detection prompt)
        """
        examples: List[str] = []

        # Build examples from parameter metadata
        param_examples = []
        if param.examples:
            # Extract clean examples (without instructional arrows)
            param_examples = [ex for ex in param.examples[:5] if "→" not in ex]
        elif param.validation and param.validation.allowed:
            # Use allowed values as examples
            param_examples = list(param.validation.allowed[:5])

        if not param_examples:
            return []

        # Generate examples based on parameter type
        for ex in param_examples[:3]:  # Show top 3 examples
            # Base example
            examples.append(f'   - "{ex}" → CREATE (parameter value)')

            # Type-specific variations
            if param.type == ParamType.STRING:
                # For string params, show space variations if example has hyphens/underscores
                if "-" in str(ex) or "_" in str(ex):
                    spaced = str(ex).replace("-", " ").replace("_", " ")
                    examples.append(f'   - "{spaced}" → CREATE (with spaces)')

        return examples

    @staticmethod
    def build_parameter_extraction_examples(
        *,
        param: ParameterMeta,
        max_examples: int = 5,
    ) -> List[str]:
        """
        Build parameter extraction examples showing input normalization.

        Generates examples demonstrating how the extractor should handle
        various user input formats (spaces, case variations, typos) and
        normalize them appropriately based on parameter type.

        Args:
            param: Parameter metadata
            max_examples: Maximum number of examples to generate

        Returns:
            List of extraction example strings (formatted for extraction prompt)
        """
        examples: List[str] = []

        # Build examples from parameter metadata
        param_examples = []
        if param.examples:
            # Extract clean examples (without instructional arrows)
            param_examples = [ex for ex in param.examples[:max_examples] if "→" not in ex]
        elif param.validation and param.validation.allowed:
            # Use allowed values as examples
            param_examples = list(param.validation.allowed[:max_examples])

        if not param_examples:
            return []

        # Determine if this is an identifier parameter (needs normalization)
        is_identifier = param.type == ParamType.STRING and any(
            keyword in str(param.key).lower() or keyword in str(param.name).lower()
            for keyword in ["identifier", "name", "bucket", "queue", "table", "key"]
        )

        # Generate extraction examples
        # BALANCED: Show base + 1 key variation per example (space variation most important)
        # Other variations (case, hyphen, underscore) covered by "Key Patterns" section
        for idx, ex in enumerate(param_examples[:3]):  # Show top 3 examples
            # For identifier params: normalize to lowercase-hyphenated
            if is_identifier:
                normalized = str(ex).lower().replace(" ", "-").replace("_", "-")
                examples.append(f'     User: "{ex}" → Extract: {param.key}="{normalized}"')

                # Check for either hyphen OR underscore (handles both SQS-style and DynamoDB-style examples)
                has_separator = "-" in str(ex) or "_" in str(ex)

                # BALANCED: Rotate variation type per example to show all types without explosion
                # Example 0: space variation (most important)
                # Example 1: case variation
                # Example 2: hyphen/underscore variation
                if idx == 0 and has_separator:
                    # Show space variation (most common user input)
                    spaced = str(ex).replace("-", " ").replace("_", " ")
                    examples.append(
                        f'     User: "{spaced}" (spaces) → Extract: {param.key}="{normalized}"'
                    )
                elif idx == 1 and str(ex).islower() and has_separator:
                    # Show uppercase variation
                    upper_case = str(ex).upper()
                    examples.append(
                        f'     User: "{upper_case}" (uppercase) → Extract: {param.key}="{normalized}"'
                    )
                elif idx == 2:
                    # Show hyphen/underscore swap variation
                    if "_" in str(ex) and "-" not in str(ex):
                        hyphenated = str(ex).replace("_", "-")
                        examples.append(
                            f'     User: "{hyphenated}" (hyphenated) → Extract: {param.key}="{normalized}"'
                        )
                    elif "-" in str(ex):
                        underscored = str(ex).replace("-", "_")
                        examples.append(
                            f'     User: "{underscored}" (underscores) → Extract: {param.key}="{normalized}"'
                        )

            # For non-identifier STRING params: extract as-is
            elif param.type == ParamType.STRING:
                examples.append(f'     User: "{ex}" → Extract: {param.key}="{ex}"')

            # For ENUM params: normalize to match value_source format
            elif param.type == ParamType.ENUM:
                # Try to match against value_source options to get canonical format
                if isinstance(param.value_source, StaticSource) and param.value_source.options:
                    # Get canonical format from value_source
                    canonical = str(ex)  # Default to example as-is
                    for option in param.value_source.options:
                        if str(option.value).lower() == str(ex).lower():
                            canonical = str(option.value)
                            break
                    examples.append(f'     User: "{ex}" → Extract: {param.key}="{canonical}"')

                    # BALANCED: Show 1 case variation per example (rotate types)
                    if canonical.isupper() and idx == 0:
                        lower_case = canonical.lower()
                        examples.append(
                            f'     User: "{lower_case}" (lowercase) → Extract: {param.key}="{canonical}"'
                        )
                    elif canonical.isupper() and idx == 1:
                        mixed_case = canonical.capitalize()
                        examples.append(
                            f'     User: "{mixed_case}" (mixed case) → Extract: {param.key}="{canonical}"'
                        )
                else:
                    # Fallback: normalize to lowercase
                    normalized = str(ex).lower()
                    examples.append(f'     User: "{ex}" → Extract: {param.key}="{normalized}"')

            elif param.type == ParamType.BOOL:
                examples.append(f'     User: "{ex}" → Extract: {param.key}={str(ex).lower()}')

            # For other types: extract as-is
            else:
                examples.append(f'     User: "{ex}" → Extract: {param.key}="{ex}"')

        # Also include instructional examples for BOOL params (natural language patterns)
        # These are examples like "need replication → enable_s3_replication=True"
        if param.type == ParamType.BOOL and param.examples:
            instructional_examples = [ex for ex in param.examples if "→" in ex]
            for inst_ex in instructional_examples[:4]:  # Limit to 4 instructional examples
                parts = inst_ex.split("→")
                if len(parts) == 2:
                    user_phrase = parts[0].strip()
                    result = parts[1].strip()
                    if "=True" in result or "=true" in result:
                        examples.append(f'     User: "{user_phrase}" → Extract: {param.key}=true')
                    elif "=False" in result or "=false" in result:
                        examples.append(f'     User: "{user_phrase}" → Extract: {param.key}=false')

        return examples

    @staticmethod
    def build_multi_value_extraction_examples(
        *,
        params: list,
        max_examples: int = 3,
    ) -> list:
        """
        Build examples showing multi-value extraction from a single user message.

        Generates examples demonstrating how to extract MULTIPLE parameters
        from a single comma-separated or space-separated user input.

        Args:
            params: List of ParameterMeta objects for the resource
            max_examples: Maximum number of multi-value examples to generate

        Returns:
            List of multi-value extraction example strings
        """
        examples = []

        if len(params) < 2:
            return examples

        # Helper to get a sample value for a parameter
        def get_sample_value(param: ParameterMeta) -> tuple:
            """Returns (user_input, extracted_value) for a parameter."""
            if param.examples:
                # Get first clean example (without arrows)
                clean_examples = [ex for ex in param.examples if "→" not in str(ex)]
                if clean_examples:
                    user_val = str(clean_examples[0])
                    # Normalize based on type
                    if param.type == ParamType.BOOL:
                        return (user_val, str(user_val).lower())
                    elif param.type == ParamType.ENUM:
                        # Get canonical from value_source if available
                        if isinstance(param.value_source, StaticSource) and param.value_source.options:
                            for option in param.value_source.options:
                                if str(option.value).lower() == user_val.lower():
                                    return (user_val, str(option.value))
                        return (user_val, user_val)
                    else:
                        # Identifier-like strings: normalize to lowercase-hyphenated
                        is_identifier = any(
                            kw in str(param.key).lower() or kw in str(param.name).lower()
                            for kw in ["identifier", "name", "bucket", "queue", "table", "key"]
                        )
                        if is_identifier:
                            normalized = user_val.lower().replace(" ", "-").replace("_", "-")
                            return (user_val, normalized)
                        return (user_val, user_val)

            # Fallback defaults by type
            if param.type == ParamType.BOOL:
                return ("true", "true")
            elif param.type == ParamType.INT:
                return ("10", "10")
            return ("value", "value")

        # Generate multi-value examples combining different parameter types
        # Example 1: First 2-3 params combined with comma
        combo_params = params[:3]
        if len(combo_params) >= 2:
            user_parts = []
            extract_parts = []
            for p in combo_params:
                user_val, extract_val = get_sample_value(p)
                # Use spaced version for identifier to show normalization
                if " " not in user_val and "-" in user_val:
                    user_val = user_val.replace("-", " ")
                user_parts.append(user_val)
                if p.type == ParamType.BOOL:
                    extract_parts.append(f'"{p.key}": {extract_val}')
                else:
                    extract_parts.append(f'"{p.key}": "{extract_val}"')

            user_input = ", ".join(user_parts)
            extract_json = "{" + ", ".join(extract_parts) + "}"
            examples.append(f'     User: "{user_input}" → Extract: {extract_json}')

        # Example 2: Mix of identifier + boolean params (common pattern)
        identifier_param = next((p for p in params if "identifier" in str(p.key).lower() or "name" in str(p.key).lower()), None)
        bool_params = [p for p in params if p.type == ParamType.BOOL][:2]

        if identifier_param and bool_params:
            user_val, extract_val = get_sample_value(identifier_param)
            if " " not in user_val and "-" in user_val:
                user_val = user_val.replace("-", " ")

            user_parts = [user_val]
            extract_parts = [f'"{identifier_param.key}": "{extract_val}"']

            for bp in bool_params:
                # Just mention the param name as a keyword
                user_parts.append(str(bp.name).lower())
                extract_parts.append(f'"{bp.key}": true')

            user_input = ", ".join(user_parts)
            extract_json = "{" + ", ".join(extract_parts) + "}"
            examples.append(f'     User: "{user_input}" → Extract: {extract_json}')

        return examples[:max_examples]

    @staticmethod
    def build_explicit_entry_examples(
        *,
        params: list,
        max_examples: int = 6,
    ) -> List[str]:
        """
        Build examples showing explicit Name:value format - NO typo correction.

        When users use explicit `Name: value` format, they are being intentional
        and precise. These examples teach the LLM to respect the exact value
        without "fixing" words that look like typos.

        Args:
            params: List of ParameterMeta objects for the resource
            max_examples: Maximum number of examples to generate

        Returns:
            List of explicit entry example strings
        """
        examples: List[str] = []

        for param in params:
            if len(examples) >= max_examples:
                break

            param_name = str(param.name)
            param_key = str(param.key)

            # Check if this is a regex/pattern parameter
            is_regex_param = (
                param.validation and
                param.validation.regex and
                param.validation.validate_as_regex
            )

            # Check if this is an identifier-like parameter
            is_identifier = any(
                kw in param_key.lower() or kw in param_name.lower()
                for kw in ["identifier", "name", "bucket", "queue", "table", "key"]
            )

            # Get a sample value from examples or generate one
            sample_value = None
            if param.examples:
                clean_examples = [ex for ex in param.examples if "→" not in str(ex)]
                if clean_examples:
                    sample_value = str(clean_examples[0])

            if not sample_value:
                sample_value = "my-value"

            # Generate explicit entry examples - showing NO typo correction
            # Include both : and = formats
            examples.append(
                f'     "{param_name}: {sample_value}" → {param_key}="{sample_value}" (exactly as provided - NO correction!)'
            )
            if len(examples) < max_examples:
                examples.append(
                    f'     "{param_name} = {sample_value}" → {param_key}="{sample_value}" (= format also respected!)'
                )

            # Add special examples for regex/pattern parameters
            if is_regex_param and len(examples) < max_examples:
                # Show a route-like value that looks like a typo but should NOT be corrected
                examples.append(
                    f'     "{param_name}: ~/api/uses$" → {param_key}="~/api/uses$" (NOT "~/api/users$" - respect exact regex!)'
                )

            # Add a "typo-looking" example for identifier params
            elif is_identifier and len(examples) < max_examples:
                # Show a value that LOOKS like a typo but should NOT be corrected
                examples.append(
                    f'     "{param_name}: tst-val" → {param_key}="tst-val" (NOT "test-val" - respect exact input!)'
                )

        return examples[:max_examples]
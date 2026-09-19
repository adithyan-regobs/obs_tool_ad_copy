"""
Route Classifier for Service Config Agent.

Single responsibility: classify user messages into route categories.
Determines whether to use legacy handlers or agentic workflow.
"""

import asyncio
import re
import time
from typing import Dict, Any, List

from langchain_core.messages import HumanMessage, SystemMessage

from app.services.langfuse_service import langfuse_service


# Route categories
ROUTE_LEGACY = "legacy"
ROUTE_AGENTIC = "agentic"
ROUTE_SIMPLE_QA = "simple_qa"


# Known parameter category keywords that should trigger agentic flow
# These are single words that indicate the user is asking about config parameters
AGENTIC_KEYWORDS = {
    # Core container parameters
    "cpu", "memory", "ram", "mem", "port", "container_port", "vcpu",

    # Health & routing
    "health", "healthcheck", "health_path", "path", "route", "routing", "service_path",

    # Scaling (ECS tasks & EKS HPA)
    "scaling", "autoscaling", "auto_scaling", "hpa", "threshold",
    "replica", "replicas", "replica_count", "desired", "desired_count",
    "min_task_count", "max_task_count", "instances",

    # HTTP scaling
    "http_scaling", "http", "https", "request_scaling", "target_value",

    # ALB / Load balancer
    "alb", "lb", "load_balancer", "priority", "listener", "rule", "rule_priority",
    "listener_rule", "listener_rule_priority", "alb_selection", "alb_schema",

    # Datadog sidecar
    "sidecar", "datadog", "dd", "datadog_sidecar", "logs_enabled", "log_source",

    # EBS storage
    "ebs", "storage", "volume", "disk", "ebs_enabled", "ebs_size", "ebs_type", "ebs_volume",

    # JVM configuration
    "jvm", "heap", "heap_size", "xms", "xmx", "initial_heap", "max_heap",
    "java", "jdk", "java_version", "version",

    # EKS resources
    "namespace", "secrets", "k8s", "kubernetes", "pod", "pods",
    "cpu_requested", "cpu_limit", "memory_requested", "memory_limit",

    # Container limits
    "ulimits", "limit", "limits", "enable_ulimits",

    # Build & deployment
    "dockerfile", "wire", "build", "build_path", "dockerfile_path",
    "generate_dockerfile", "wire_enabled", "wire_path",

    # Repository & CI/CD
    "repository", "repo", "branches", "branch", "github", "git",
    "trigger", "paths", "other_paths", "trigger_paths",

    # Build tools
    "gradle", "maven", "snyk", "tests", "skip_tests", "skip_checks",
    "skip_code_quality", "skip_snyk_code", "skip_snyk_oss", "skip_snyk_container",
    "gradle_jar", "gradle_tasks", "gradle_workers", "maven_jar", "maven_goals", "maven_profile",

    # Deployment strategies
    "canary", "bluegreen", "blue_green", "rolling", "recreate", "strategy",
    "deployment_strategy", "canary_steps", "canary_analysis",
    "rolling_max_surge", "rolling_max_unavailable",

    # Language & runtime
    "language", "lang", "framework", "runtime", "tech_stack",

    # Go-specific
    "go", "golang", "go_config", "go_secrets", "go_use_aws_secrets", "go_config_path",

    # Notifications
    "slack", "slack_channel", "notifications",

    # Build types
    "eks_build", "build_type", "jar_file", "jvm_args",
}


# Patterns for quick classification (no LLM needed)
LEGACY_PATTERNS = [
    # Service selection
    r"^(list|show)\s*(all\s*)?(services?|svcs?)",
    r"^(select|choose|use|pick)\s+",
    # Form filling
    r"^(yes|no|yep|nope|sure|ok|okay)$",
    r"^(yes|no),?\s*(fill|use|set)",
    r"^fill\s*(the\s*)?(form|config)",
    # Help
    r"^(help|what can you do|commands?)\s*\??$",
    # Note: Removed "cpu?", "ram?", etc. - these now go through agentic
    # for infrastructure-aware responses (ECS vs EKS)
]

AGENTIC_PATTERNS = [
    # Listener priority queries (specific patterns only)
    r"listener.*(priority|rule)",  # "listener priority", "listener rule priority"
    r"(priority|rule).*listener",  # "priority for listener"
    r"is\s+\d+\s+(ok|okay|available|valid).*(listener|priority)",  # "is 50 okay for listener priority"
    r"can\s+i\s+use\s+\d+.*(listener|priority)",  # "can I use 100 for listener"
    r"give\s+me\s+(a\s+)?listener\s*(priority|rule)",  # "give me a listener priority"
    # Parameter category queries (single words or phrases)
    r"^(sidecar|datadog|dd|ebs|jvm|alb|scaling|autoscaling|ulimits)\s*(config|configs|configuration|settings|params|parameters)?\s*\??$",
    r"^(datadog|dd)\s+(sidecar|config|cpu|memory|mem)\s*\??$",
    r"^(enable|disable)\s+(sidecar|datadog|ebs|autoscaling)\s*\??$",
    # Parameter/service definition queries (updated to handle multi-word phrases)
    r"^what\s+(is|are)\s+[\w_\s-]+\s*\??$",  # "what is cpu", "what is sidecar config"
    r"^what\s+(is|are)\s+(the\s+)?[\w_-]+\s+service\s*\??$",  # "what is baa service"
    r"^what\s+(is|are)\s+(the\s+)?[\w_-]+\s+(config|configuration)\s*\??$",  # "what is baa config"
    r"^tell\s+me\s+about\s+[\w_\s-]+\s*\??$",  # "tell me about cpu", "tell me about sidecar configs"
    r"^tell\s+me\s+about\s+(the\s+)?[\w_-]+\s+service\s*\??$",  # "tell me about baa service"
    # Specific service queries
    r"what\s+(is|are)\s+.+\s+for\s+.+",
    r"(cpu|memory|ram|port)\s+for\s+\w+",
    r"(cpu|memory|ram|port)\s+(value|in|of)\s+",
    # Comparisons
    r"compare\s+.+\s+(to|with|vs|versus)\s+",
    r"difference\s+between\s+.+\s+and\s+",
    r"(staging|prod|dev)\s+vs\s+(staging|prod|dev)",
    # Discovery queries
    r"which\s+services?\s+(have|has|use|with)",
    r"find\s+(all\s+)?services?\s+",
    r"list\s+services?\s+(with|that|where)",
    # Status queries
    r"is\s+\w+\s+deployed",
    r"deployment\s+status\s+(of|for)",
    r"(what|which)\s+services?\s+depend",
    # Follow-up queries (continuation of agentic queries)
    r"^(how|what)\s+about\s+",
    r"^and\s+(for|in)\s+",
    r"^(in|for)\s+\w+",
    r"^same\s+(for|in)\s+",
]


class RouteClassifier:
    """
    Classifies messages into route categories.

    Responsibilities:
    - Quick pattern matching for obvious cases
    - LLM fallback for ambiguous cases
    - Route determination (legacy/agentic/simple_qa)
    """

    def __init__(self, llm):
        """
        Initialize with LLM client.

        Args:
            llm: LangChain chat client for classification
        """
        self.llm = llm
        self._legacy_patterns = [re.compile(p, re.IGNORECASE) for p in LEGACY_PATTERNS]
        self._agentic_patterns = [re.compile(p, re.IGNORECASE) for p in AGENTIC_PATTERNS]

    async def classify(
        self,
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]] = None,
    ) -> str:
        """
        Classify message into route category.

        Args:
            message: User's message
            context: State context (reference_service, etc.)
            history: Conversation history

        Returns:
            Route category: "legacy", "agentic", or "simple_qa"
        """
        history = history or []
        message_lower = message.lower().strip()

        # Quick check: empty or very short messages
        if len(message_lower) < 2:
            return ROUTE_LEGACY

        # Quick check: confirmation/denial patterns
        # Route based on what the user is confirming
        if message_lower in {"yes", "no", "yep", "nope", "sure", "ok", "okay", "y", "n"}:
            if self._has_pending_clarification(history):
                # User is responding to "Did you mean X?" - route to agentic for retry
                return ROUTE_AGENTIC
            if self._has_pending_autofill_offer(history):
                # User is confirming/denying autofill - route to legacy form_fill
                return ROUTE_LEGACY
            # Just an acknowledgment (e.g., "okay" after info message) - not a form action
            return ROUTE_SIMPLE_QA

        # Check for value proposal follow-ups (e.g., "can i use 350" after listener priority offer)
        if self._is_value_proposal(message_lower) and self._has_pending_autofill_offer(history):
            return ROUTE_AGENTIC

        # Check for service selection after agentic service_ambiguity
        # (e.g., user clicks "amal-serr" after compare query showed ambiguity)
        if self._has_pending_agentic_ambiguity(history):
            # This looks like a service name selection - route to agentic to retry original query
            return ROUTE_AGENTIC

        # Pattern matching for legacy
        for pattern in self._legacy_patterns:
            if pattern.search(message_lower):
                return ROUTE_LEGACY

        # Pattern matching for agentic
        for pattern in self._agentic_patterns:
            if pattern.search(message_lower):
                return ROUTE_AGENTIC

        # Check for parameter category keywords (single words like "sidecar", "ebs")
        words = set(message_lower.split())
        if words & AGENTIC_KEYWORDS:
            return ROUTE_AGENTIC

        # Context-based classification
        if context.get("pending_fill"):
            # If we're in the middle of form filling, stay in legacy
            return ROUTE_LEGACY

        # LLM fallback for ambiguous cases
        return await self._classify_with_llm(message, context, history)

    async def _classify_with_llm(
        self,
        message: str,
        context: Dict[str, Any],
        history: List[Dict[str, str]],
    ) -> str:
        """Use LLM to classify ambiguous messages."""
        prompt = f"""Classify the user's message into one of three categories:

Context:
- Reference service selected: {context.get('reference_service', 'none')}
- Has existing config: {context.get('reference_has_config', False)}

User message: "{message}"

Categories:
1. LEGACY - Form filling, service selection, simple parameter questions, confirmations
   Examples: "list services", "yes", "cpu?", "select payment-service", "fill form"

2. AGENTIC - Queries requiring data lookup: comparisons, searches, status checks, cross-service queries
   Examples: "What is CPU for payment-service?", "Compare staging vs prod", "Which services have autoscaling?"

3. SIMPLE_QA - Greetings, help requests, general questions
   Examples: "hi", "hello", "what can you do?", "help"

Respond with ONLY one word: LEGACY, AGENTIC, or SIMPLE_QA"""

        messages = [
            SystemMessage(content="You classify messages. Respond with only: LEGACY, AGENTIC, or SIMPLE_QA"),
            HumanMessage(content=prompt),
        ]

        start_time = time.perf_counter()
        response = await self.llm.ainvoke(messages)
        result = response.content.strip().upper()
        latency_ms = (time.perf_counter() - start_time) * 1000

        # Fire-and-forget: Log classification
        asyncio.create_task(langfuse_service.log_llm_call(
            call_type="route_classification",
            input_text=message[:200],
            output_text=result,
            latency_ms=latency_ms,
        ))

        # Validate and return
        if result == "AGENTIC":
            return ROUTE_AGENTIC
        elif result == "SIMPLE_QA":
            return ROUTE_SIMPLE_QA
        else:
            return ROUTE_LEGACY

    def _has_pending_clarification(self, history: List[Dict[str, str]]) -> bool:
        """
        Check if last agent message was a clarification question.

        Looks for patterns like "Did you mean 'X'?" which indicate
        the agent asked for confirmation of a suggested parameter.
        """
        if not history:
            return False

        # Find last agent message
        for msg in reversed(history):
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == "agent":
                message = msg.get('message', '') if isinstance(msg, dict) else getattr(msg, 'message', '')
                # Check for clarification patterns
                if "Did you mean" in message:
                    return True
                break  # Only check most recent agent message

        return False

    def _is_value_proposal(self, message: str) -> bool:
        """
        Check if message is proposing a value (e.g., "can i use 350", "how about 200").
        """
        # Patterns for value proposals
        value_patterns = [
            r"can\s+i\s+(use|choose|set|pick)\s+\d+",
            r"(how|what)\s+about\s+\d+",
            r"is\s+\d+\s+(ok|okay|good|valid|available)",
            r"^use\s+\d+",
            r"^set\s+(it\s+)?to\s+\d+",
        ]
        for pattern in value_patterns:
            if re.search(pattern, message, re.IGNORECASE):
                return True
        return False

    def _has_pending_autofill_offer(self, history: List[Dict[str, str]]) -> bool:
        """
        Check if there's a recent autofill offer in history.

        Looks for "Would you like me to fill 'X' with value 'Y'?" or
        "Would you like to fill the form with this config?" patterns
        in recent agent messages (within last 4 messages).
        """
        if not history:
            return False

        # Check last few messages for autofill offer
        agent_count = 0
        for msg in reversed(history):
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == "agent":
                agent_count += 1
                if agent_count > 2:  # Only check last 2 agent messages
                    break
                message = msg.get('message', '') if isinstance(msg, dict) else getattr(msg, 'message', '')
                # Match both "Would you like me to fill" and "Would you like to fill"
                if "Would you like" in message and "fill" in message:
                    return True

        return False

    def _has_pending_agentic_ambiguity(self, history: List[Dict[str, str]]) -> bool:
        """
        Check if there's a pending agentic service ambiguity in history.

        Looks for "I found multiple services matching your query" pattern
        in recent agent messages, indicating user needs to select a service
        to continue an agentic query (like compare).
        """
        if not history:
            return False

        # Check last agent message for agentic service ambiguity
        for msg in reversed(history):
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == "agent":
                message = msg.get('message', '') if isinstance(msg, dict) else getattr(msg, 'message', '')
                # Check for agentic service ambiguity pattern
                if "I found multiple services matching your query" in message:
                    return True
                break  # Only check most recent agent message

        return False

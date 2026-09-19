"""
Jenkins Console Log Parser

Parses ALL pipeline stages from raw Jenkins console output, using
``[Pipeline] { (Stage Name)`` markers as stage boundaries.  Every stage's
shell commands and their output are captured so the UI can display the
full build story — not just Docker lines.

Handles:
- ANSI escape sequences (Jenkins console annotations)
- Stage boundary detection via ``[Pipeline] { (StageName)``
- Nested / wrapper stages — only leaf stages with real content are kept
- Shell command lines (``+ cmd``) tagged with status="command"
- Docker BuildKit and classic Docker build output (rich formatting)
- Skipped stages (``skipped due to earlier failure``)
- Failed-stage detection (error patterns)
- Webhook / curl noise filtering
- Parallel branch interleaving and deduplication
- Progressive / partial logs (last stage stays open)

Usage:
    from app.services.jenkins_log_parser import parse_jenkins_log
    result = parse_jenkins_log(raw_text)
    # result = {
    #   "stages": [
    #     {"name": "Build", "status": "failed", "lines": [...]},
    #     {"name": "Deploy", "status": "skipped", "lines": []},
    #   ]
    # }
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ANSI / Jenkins console annotation stripping
# ---------------------------------------------------------------------------
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[8m.*?\x1b\[0m")
_ANSI_CODE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# ---------------------------------------------------------------------------
# Pipeline markers
# ---------------------------------------------------------------------------
_PIPELINE_BRACE_STAGE_RE = re.compile(r"^\[Pipeline\]\s*\{\s*\((.+?)\)\s*$")
# Bare `[Pipeline] {` opens a sub-block (script {}, dir {}, retry {}, etc.) —
# distinct from a named-stage open. Each such open has a matching `[Pipeline] }`
# that must NOT close the surrounding stage.
_PIPELINE_BARE_BRACE_RE = re.compile(r"^\[Pipeline\]\s*\{\s*$")
_PIPELINE_CLOSE_BRACE_RE = re.compile(r"^\[Pipeline\]\s*\}\s*$")
_PIPELINE_ANY_RE = re.compile(r"^\[Pipeline\]\s+")

# ---------------------------------------------------------------------------
# Webhook / curl noise
# ---------------------------------------------------------------------------
_WEBHOOK_CURL_RE = re.compile(r"curl.*webhook", re.IGNORECASE)
# Matches FastAPI response bodies returned by the webhook endpoint — both the
# happy path (`{"status":"ok", ...}`) and validation errors
# (`{"detail":[{"type":"json_invalid", ...}]}`) that occur when the Jenkinsfile
# POSTs malformed JSON. Without this, the error body leaks into the user's log.
_WEBHOOK_JSON_RE = re.compile(
    r'^\s*\{\s*"(?:status|detail|pipeline_code)"\s*:'
)
_WEBHOOK_MARKERS = ("OBSTOOL_WEBHOOK_URL", "X-Webhook-Secret", "jenkins-webhook")

# ---------------------------------------------------------------------------
# Failure / skip detection
# ---------------------------------------------------------------------------
_FAILURE_PATTERNS = [
    re.compile(r"not found", re.IGNORECASE),
    re.compile(r"No such file or directory", re.IGNORECASE),
    re.compile(r"can't read", re.IGNORECASE),
    re.compile(r"exit code", re.IGNORECASE),
    re.compile(r"returned exit code", re.IGNORECASE),
    re.compile(r"\bFAILURE\b"),
    re.compile(r"\bfatal\b", re.IGNORECASE),
    re.compile(r"\berror:", re.IGNORECASE),
]
# Lines that match _FAILURE_PATTERNS but are actually harmless (false positives).
# e.g. kubectl rollout status --timeout=5s prints "error: timed out waiting for the
# condition" on every polling attempt before the rollout finishes — not a real failure.
_FAILURE_FALSE_POSITIVES = [
    re.compile(r"error:\s*timed out waiting for the condition", re.IGNORECASE),
    re.compile(r"Waiting for deployment", re.IGNORECASE),
]
_SKIPPED_RE = re.compile(r"skipped due to earlier failure", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Wrapper stage names that should be collapsed to their children
# ---------------------------------------------------------------------------
_WRAPPER_STAGE_NAMES = {
    "Build & Provision Infra",
    "Build JAR + Docker + Push",
    "Docker Build + Push",
}

# Stages that should always be hidden from the user
_HIDDEN_STAGE_NAMES = {
    "Declarative: Post Actions",
    "Declarative: Checkout SCM",
}

# ---------------------------------------------------------------------------
# Stages that show full build logs (all others get friendly summaries)
# ---------------------------------------------------------------------------
_BUILD_LOG_STAGES = {"Build", "Build JAR", "Docker Build & Push", "Docker Build + Push"}
# Infra-apply stages emit Terragrunt output. Only the `Applying` stage carries
# the content users actually need (Plan, resource events, Apply summary,
# errors). Init, Cloning Repo, Capturing Outputs, Verifying Permissions all
# get friendly one-liners — their raw output is provider-download / shell
# command noise.
_INFRA_APPLY_LOG_STAGES = {
    "Applying",
}

# ---------------------------------------------------------------------------
# Terragrunt-specific patterns (applied inside _parse_generic_lines)
# ---------------------------------------------------------------------------
# Each terraform line ships as `HH:MM:SS.sss STDOUT terraform: <content>` after
# ANSI strip. Pull the timestamp out and surface only the content.
_TF_PREFIX_RE = re.compile(r"^(\d{1,2}:\d{2}:\d{2}(?:\.\d+)?)\s+STDOUT\s+terraform:\s?")
_TF_PLAN_RE = re.compile(r"^Plan:\s+\d+ to add, \d+ to change, \d+ to destroy\.")
_TF_APPLY_RE = re.compile(r"^Apply complete!\s+Resources:\s+\d+ added, \d+ changed, \d+ destroyed\.")
# Warnings come wrapped in box-drawing chars (╷ │ ╵). Match either the bare
# "Warning:" line or its gutter continuation.
_TF_WARNING_HEADER_RE = re.compile(r"^[│╷]?\s*Warning:\s")
_TF_BLOCK_GUTTER_RE = re.compile(r"^[│╷╵]")

# Regex helpers for extracting context from raw lines
# Matches both old-style `git clone --branch X ... repo.git .`
# and new-style `git fetch origin BRANCH` + `git remote add origin ... repo.git`
_GIT_CLONE_RE = re.compile(r"git clone\s+--branch\s+(\S+)\s+.*github\.com[/:]([^\s]+?)\.git")
_GIT_FETCH_RE = re.compile(r"git fetch\s+origin\s+(\S+)")
_GIT_REMOTE_RE = re.compile(r"git remote add\s+origin\s+.*github\.com[/:]([^\s]+?)\.git")
_KUBECTL_APPLY_RE = re.compile(r"kubectl apply -f\s+\S+/(\S+\.yaml)")
_DOCKER_PUSH_RE = re.compile(r"docker push\s+(\S+)")
_DOCKER_TAG_RE = re.compile(r"docker tag\s+\S+\s+(\S+)")
_ECR_REPO_RE = re.compile(r"aws ecr (?:describe|create)-repositor(?:ies|y)\s+--repository-names?\s+(\S+)")
_ROLLOUT_RE = re.compile(r"kubectl rollout status\s+deployment/(\S+)")
_ALB_HOSTNAME_RE = re.compile(r"ALB hostname ready:\s*(\S+)")
# Helm install/upgrade detection (for helm-chart pipelines)
_HELM_INSTALL_RE = re.compile(r"helm\s+(?:install|upgrade)\s+(\S+)")
# Model download detection (huggingface-cli / aws s3 cp / python download script)
_HF_DOWNLOAD_RE = re.compile(r"(?:huggingface-cli\s+download|from\s+huggingface_hub)\s+(\S+)?", re.IGNORECASE)
_MODEL_ID_RE = re.compile(r'"model_id"\s*:\s*"([^"]+)"')

# ---------------------------------------------------------------------------
# Docker BuildKit patterns (kept for rich formatting)
# ---------------------------------------------------------------------------
_BUILDKIT_STEP_RE = re.compile(
    r"^#(\d+)\s+\[(?:([\w.-]+)\s+)?(\d+/\d+)\]\s+(.+)"
)
_BUILDKIT_ARROW_RE = re.compile(
    r"^=>\s+\[(?:([\w.-]+)\s+)?(\d+/\d+)\]\s+(.+?)(?:\s+([\d.]+)s)?\s*$"
)
_BUILDKIT_CACHED_RE = re.compile(
    r"^=>\s+CACHED\s+\[(?:([\w.-]+)\s+)?(\d+/\d+)\]\s+(.+)"
)
_BUILDKIT_INTERNAL_RE = re.compile(r"^#(\d+)\s+\[internal\]\s+(.+)")
_BUILDKIT_SUB_RE = re.compile(
    r"^#(\d+)\s+(?:DONE|transferring|building with|exporting|writing|naming|sha256:|resolve)\s*.*"
)
_BUILDKIT_SUMMARY_RE = re.compile(r"^[✓✗]\s+\d+/\d+.+")
_DOCKER_BUILD_RE = re.compile(r"docker build\s+")
_CLASSIC_STEP_RE = re.compile(r"^Step\s+(\d+/\d+)\s*:\s*(.+)")

# BuildKit compact/TTY format: bare Dockerfile instructions without #N prefix
_DOCKERFILE_CMDS = (
    "FROM", "RUN", "COPY", "ADD", "WORKDIR", "ENV", "EXPOSE", "CMD",
    "ENTRYPOINT", "ARG", "LABEL", "USER", "VOLUME", "SHELL",
    "HEALTHCHECK", "ONBUILD", "STOPSIGNAL",
)
_BARE_INSTRUCTION_RE = re.compile(
    r"^(" + "|".join(_DOCKERFILE_CMDS) + r")\s+(.+)", re.IGNORECASE,
)
# BuildKit compact internal lines: "load build definition ...", "load .dockerignore"
_BARE_INTERNAL_RE = re.compile(r"^load\s+(.+)", re.IGNORECASE)
# BuildKit compact cached marker: "#N CACHED" (no DONE, just CACHED)
_BUILDKIT_BARE_CACHED_RE = re.compile(r"^#(\d+)\s+CACHED\s*$")
# Bare duration line: "0.1s", "54.4s"
_BARE_DURATION_RE = re.compile(r"^([\d.]+)s\s*$")

# Shell command pattern
_SHELL_CMD_RE = re.compile(r"^\+\s+(.+)")

# Non-docker keywords (for parallel branch interleaving)
_NON_DOCKER_KEYWORDS = [
    "kubectl ", "namespace/", "serviceaccount/", "ingressclass",
    "service/", "ingress.", "aws ", "Updated context",
    "unchanged", "created", "configured",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class LogLine:
    """A single parsed log line."""
    message: str
    timestamp: Optional[str] = None
    step_num: Optional[str] = None
    command: Optional[str] = None
    status: Optional[str] = None  # "done", "cached", "running", "command"
    duration: Optional[str] = None
    docker_stage: Optional[str] = None  # e.g. "build", "stage-1" for multi-stage builds
    sub_lines: List[str] = field(default_factory=list)


@dataclass
class Stage:
    """A parsed pipeline stage with its log lines."""
    name: str
    status: str = "success"  # "success", "failed", "skipped"
    lines: List[LogLine] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "lines": [],
        }
        for line in self.lines:
            entry: Dict[str, Any] = {"message": line.message}
            if line.timestamp:
                entry["timestamp"] = line.timestamp
            if line.step_num:
                entry["step_num"] = line.step_num
            if line.command:
                entry["command"] = line.command
            if line.status:
                entry["status"] = line.status
            if line.duration:
                entry["duration"] = line.duration
            if line.docker_stage:
                entry["docker_stage"] = line.docker_stage
            if line.sub_lines:
                entry["sub_lines"] = line.sub_lines
            result["lines"].append(entry)
        return result


# ---------------------------------------------------------------------------
# Helpers (kept from the original parser)
# ---------------------------------------------------------------------------

def _strip_ansi(text: str) -> str:
    """Remove Jenkins console annotations and ANSI escape sequences."""
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _ANSI_CODE_RE.sub("", text)
    return text


def _deduplicate_lines(lines: List[str], skip_docker: bool = False) -> List[str]:
    """Remove duplicate lines from parallel interleaved output.

    When *skip_docker* is True, lines that look like Docker build content
    (bare Dockerfile instructions, BuildKit steps, etc.) are never deduplicated
    because multi-stage builds legitimately repeat identical instructions
    (e.g. ``WORKDIR /app`` in both the build and runtime stages).
    """
    if len(lines) <= 1:
        return lines

    result: List[str] = []
    recent: List[str] = []
    window_size = 50

    for line in lines:
        stripped = line.rstrip()
        if stripped in recent:
            # Never dedup docker content — multi-stage builds repeat instructions
            if not (skip_docker and _is_docker_content(stripped)):
                continue
        result.append(line)
        recent.append(stripped)
        if len(recent) > window_size:
            recent.pop(0)

    return result


def _is_docker_content(line: str) -> bool:
    """Check if a line is part of Docker build output."""
    return bool(
        _BUILDKIT_STEP_RE.match(line)
        or _BUILDKIT_ARROW_RE.match(line)
        or _BUILDKIT_CACHED_RE.match(line)
        or _BUILDKIT_INTERNAL_RE.match(line)
        or _BUILDKIT_SUB_RE.match(line)
        or _BUILDKIT_SUMMARY_RE.match(line)
        or _CLASSIC_STEP_RE.match(line)
        or _BARE_INSTRUCTION_RE.match(line)
        or _BARE_INTERNAL_RE.match(line)
        or _BUILDKIT_BARE_CACHED_RE.match(line)
        or _BARE_DURATION_RE.match(line)
    )


def _extract_duration_from_sub_lines(sub_lines: List[str]) -> Optional[str]:
    """Extract duration from '#N DONE X.Xs' sub-lines."""
    for sl in sub_lines:
        m = re.match(r"^#\d+\s+DONE\s+([\d.]+)s", sl)
        if m:
            return f"{m.group(1)}s"
    return None


def _parse_docker_lines(raw_lines: List[str]) -> List[LogLine]:
    """
    Parse docker build lines into structured LogLine entries.

    Preserves ALL sub-output (pip install logs, apt output, etc.)
    as sub_lines on the parent step.  Internal steps get step_num="internal".

    BuildKit often outputs the same steps twice: first as ``#N [step/total]``
    and then as ``=> [step/total]`` with durations.  We deduplicate by merging
    arrow-format lines into already-seen step_num entries.
    """
    parsed: List[LogLine] = []
    current_step: Optional[LogLine] = None
    current_bk_id: Optional[str] = None
    # Track step_num -> LogLine for dedup (arrow lines merge into existing)
    seen_steps: Dict[str, LogLine] = {}

    def _seen_key(docker_stage: Optional[str], step_num: str) -> str:
        """Build a dedup key that includes the Docker stage name to avoid
        collisions across multi-stage builds (e.g. 'build:1/5' vs 'stage-1:1/3')."""
        return f"{docker_stage or ''}:{step_num}"

    def _flush_step() -> None:
        nonlocal current_step, current_bk_id
        if current_step:
            if not current_step.duration:
                current_step.duration = _extract_duration_from_sub_lines(current_step.sub_lines)
            # Only append if not already tracked via seen_steps
            sk = _seen_key(current_step.docker_stage, current_step.step_num) if current_step.step_num else None
            if sk and sk in seen_steps:
                # Already in parsed via seen_steps — skip appending again
                pass
            else:
                parsed.append(current_step)
                if current_step.step_num and current_step.step_num != "internal" and sk:
                    seen_steps[sk] = current_step
            current_step = None
            current_bk_id = None

    for raw in raw_lines:
        line = raw.rstrip()

        # Docker build command itself
        if _DOCKER_BUILD_RE.search(line):
            _flush_step()
            m = _SHELL_CMD_RE.match(line)
            cmd = m.group(1).strip() if m else line
            parsed.append(LogLine(message=line, command=cmd, status="command"))
            continue

        # BuildKit internal step
        m = _BUILDKIT_INTERNAL_RE.match(line)
        if m:
            _flush_step()
            current_bk_id = m.group(1)
            current_step = LogLine(
                message=line, step_num="internal",
                command=m.group(2).strip(), status="done",
            )
            continue

        # BuildKit cached step — may be a dup of an existing step_num
        m = _BUILDKIT_CACHED_RE.match(line)
        if m:
            docker_stage = m.group(1)  # e.g. "build", "stage-1", or None
            step_num = m.group(2)
            sk = _seen_key(docker_stage, step_num)
            if sk in seen_steps:
                # Merge: update existing entry to cached
                seen_steps[sk].status = "cached"
                _flush_step()  # discard current_step if any
                continue
            _flush_step()
            current_step = LogLine(
                message=line, step_num=step_num,
                command=m.group(3).strip(), status="cached",
                docker_stage=docker_stage,
            )
            current_bk_id = None
            continue

        # BuildKit arrow step — often a dup of #N step with added duration
        m = _BUILDKIT_ARROW_RE.match(line)
        if m:
            docker_stage = m.group(1)  # e.g. "build", "stage-1", or None
            step_num = m.group(2)
            duration = f"{m.group(4)}s" if m.group(4) else None
            sk = _seen_key(docker_stage, step_num)
            if sk in seen_steps:
                # Merge duration into existing entry
                if duration:
                    seen_steps[sk].duration = duration
                _flush_step()  # discard current_step if any
                continue
            _flush_step()
            current_step = LogLine(
                message=line, step_num=step_num,
                command=m.group(3).strip(), status="done",
                duration=duration, docker_stage=docker_stage,
            )
            current_bk_id = None
            continue

        # BuildKit numbered step
        m = _BUILDKIT_STEP_RE.match(line)
        if m:
            _flush_step()
            current_bk_id = m.group(1)
            docker_stage = m.group(2)  # e.g. "build", "stage-1", or None
            current_step = LogLine(
                message=line, step_num=m.group(3),
                command=m.group(4).strip(), status="done",
                docker_stage=docker_stage,
            )
            continue

        # Classic Docker step
        m = _CLASSIC_STEP_RE.match(line)
        if m:
            _flush_step()
            current_step = LogLine(
                message=line, step_num=m.group(1),
                command=m.group(2).strip(), status="done",
            )
            current_bk_id = None
            continue

        # BuildKit compact: "#N CACHED" (bare cached marker)
        m = _BUILDKIT_BARE_CACHED_RE.match(line)
        if m:
            if current_step:
                current_step.status = "cached"
                current_step.sub_lines.append(line)
            continue

        # BuildKit compact: bare Dockerfile instruction (FROM, RUN, COPY, etc.)
        m = _BARE_INSTRUCTION_RE.match(line)
        if m:
            _flush_step()
            instruction = m.group(1).upper()
            full_cmd = f"{instruction} {m.group(2).strip()}"
            # Infer docker_stage from COPY --from=<stage> or FROM ... AS <stage>
            ds = None
            if instruction == "COPY":
                from_match = re.search(r"--from=(\S+)", m.group(2))
                if from_match:
                    # This step is in the stage that copies FROM another stage
                    # (i.e. the runtime/final stage), not the source stage
                    pass
            current_step = LogLine(
                message=line, command=full_cmd, status="done",
                docker_stage=ds,
            )
            current_bk_id = None
            continue

        # BuildKit compact: bare internal line ("load build definition ...")
        m = _BARE_INTERNAL_RE.match(line)
        if m:
            _flush_step()
            current_step = LogLine(
                message=line, step_num="internal",
                command=m.group(1).strip(), status="done",
            )
            current_bk_id = None
            continue

        # BuildKit compact: bare duration line ("0.1s", "54.4s")
        m = _BARE_DURATION_RE.match(line)
        if m:
            if current_step:
                current_step.duration = f"{m.group(1)}s"
                current_step.sub_lines.append(line)
            continue

        # BuildKit summary
        if _BUILDKIT_SUMMARY_RE.match(line):
            _flush_step()
            parsed.append(LogLine(message=line, status="done"))
            continue

        # BuildKit sub-output
        if _BUILDKIT_SUB_RE.match(line):
            if current_step:
                current_step.sub_lines.append(line)
            else:
                parsed.append(LogLine(message=line, status="done"))
            continue

        # Sub-output for the current step
        if current_step:
            current_step.sub_lines.append(line)
        elif line.strip():
            parsed.append(LogLine(message=line))

    _flush_step()
    return parsed


# ---------------------------------------------------------------------------
# Webhook noise detection
# ---------------------------------------------------------------------------

def _is_webhook_noise(line: str) -> bool:
    """Return True if the line is webhook curl noise that should be hidden."""
    stripped = line.strip()
    if not stripped:
        return False

    # Lines starting with + curl ... webhook
    if _SHELL_CMD_RE.match(stripped) and _WEBHOOK_CURL_RE.search(stripped):
        return True

    # Raw curl lines (without + prefix) that mention webhook
    if stripped.startswith("curl") and _WEBHOOK_CURL_RE.search(stripped):
        return True

    # JSON status responses from the webhook
    if _WEBHOOK_JSON_RE.match(stripped):
        return True

    # Lines referencing our webhook markers
    if any(marker in stripped for marker in _WEBHOOK_MARKERS):
        return True

    return False


# ---------------------------------------------------------------------------
# Stage-level failure detection
# ---------------------------------------------------------------------------

def _has_failure_pattern(line: str) -> bool:
    """Return True if the line contains a known failure indicator."""
    if not any(p.search(line) for p in _FAILURE_PATTERNS):
        return False
    # Exclude known false positives (e.g. kubectl polling timeout messages)
    if any(fp.search(line) for fp in _FAILURE_FALSE_POSITIVES):
        return False
    return True


# ---------------------------------------------------------------------------
# Detect whether a stage contains Docker build output
# ---------------------------------------------------------------------------

def _stage_has_docker_content(raw_lines: List[str]) -> bool:
    """Return True if any line in the batch is Docker build output."""
    return any(_is_docker_content(l.rstrip()) or _DOCKER_BUILD_RE.search(l) for l in raw_lines)


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_jenkins_log(raw_text: str) -> Dict[str, Any]:
    """
    Parse raw Jenkins console text and extract ALL pipeline stages.

    Walks through lines looking for ``[Pipeline] { (StageName)`` to detect
    stage boundaries.  All ``[Pipeline]`` infrastructure lines are skipped.
    Webhook curl noise is filtered.  Shell commands are tagged with
    ``status="command"``.  Docker BuildKit output gets rich formatting via
    ``_parse_docker_lines()``.

    Returns::

        {
          "stages": [
            {"name": "Build", "status": "failed", "lines": [...]},
            {"name": "Deploy", "status": "skipped", "lines": []},
          ]
        }
    """
    if not raw_text.strip():
        return {"stages": []}

    # --- Step 1: Strip ANSI, split into lines ---
    cleaned = _strip_ansi(raw_text)
    all_lines = cleaned.split("\n")

    logger.info(
        "parse_jenkins_log: %d raw chars, %d lines after ANSI strip",
        len(raw_text), len(all_lines),
    )

    # --- Step 2: Walk lines, collect into stages ---
    stages: List[Stage] = []
    # Stack of (stage, raw_lines, brace_depth) — used for nested stage handling
    stage_stack: List[tuple] = []  # (Stage, List[str], int)
    current_stage: Optional[Stage] = None
    current_raw: List[str] = []
    # Depth of bare `[Pipeline] {` blocks (script/dir/retry/withEnv/etc.) opened
    # *inside* the current stage. Each bare close decrements; only when depth is
    # zero does a `[Pipeline] }` close the surrounding stage.
    nested_brace_depth = 0
    # Lines that appear outside any stage (before first stage / after last close)
    orphan_lines: List[str] = []

    for line in all_lines:
        stripped = line.rstrip()

        # --- Stage open: [Pipeline] { (StageName) ---
        m = _PIPELINE_BRACE_STAGE_RE.match(stripped)
        if m:
            stage_name = m.group(1).strip()

            # If we're in a wrapper stage, push it onto the stack
            if current_stage is not None:
                stage_stack.append((current_stage, current_raw, nested_brace_depth))

            current_stage = Stage(name=stage_name)
            current_raw = []
            nested_brace_depth = 0
            continue

        # --- Bare brace open: [Pipeline] { (no parens) — sub-block, not a stage ---
        if _PIPELINE_BARE_BRACE_RE.match(stripped):
            if current_stage is not None:
                nested_brace_depth += 1
            continue

        # --- Brace close: [Pipeline] } ---
        if _PIPELINE_CLOSE_BRACE_RE.match(stripped):
            if current_stage is not None:
                if nested_brace_depth > 0:
                    # Inner sub-block close — stay in the current stage.
                    nested_brace_depth -= 1
                    continue
                # Stage close
                _finalise_stage(current_stage, current_raw, stages)

                # Pop parent from stack if any
                if stage_stack:
                    current_stage, current_raw, nested_brace_depth = stage_stack.pop()
                else:
                    current_stage = None
                    current_raw = []
                    nested_brace_depth = 0
            continue

        # --- Skip all other [Pipeline] lines ---
        if _PIPELINE_ANY_RE.match(stripped):
            continue

        # --- Collect line into current stage (or orphan bucket) ---
        if current_stage is not None:
            current_raw.append(line)
        else:
            orphan_lines.append(line)

    # --- Handle progressive / partial logs: last stage may still be open ---
    if current_stage is not None:
        _finalise_stage(current_stage, current_raw, stages)
        # Drain the stack
        while stage_stack:
            parent_stage, parent_raw = stage_stack.pop()
            _finalise_stage(parent_stage, parent_raw, stages)

    # --- Handle orphan lines (e.g., trailing "Finished: FAILURE") ---
    # We don't create a stage for orphans; they're pipeline-level noise.

    # --- Filter out wrapper stages that ended up with no lines ---
    # (wrapper stages whose children were already extracted)
    final_stages = [s for s in stages if s.name not in _WRAPPER_STAGE_NAMES or s.lines]

    # --- Filter truly empty stages (no lines and not skipped) ---
    final_stages = [
        s for s in final_stages
        if s.lines or s.status == "skipped"
    ]

    return {"stages": [s.to_dict() for s in final_stages]}


def _finalise_stage(stage: Stage, raw_lines: List[str], output: List[Stage]) -> None:
    """
    Process the raw lines collected for a stage, build LogLine entries,
    detect failure/skip, and append to the output list.
    """
    # --- Always-hidden stages (e.g. Declarative: Post Actions) ---
    if stage.name in _HIDDEN_STAGE_NAMES:
        return

    # --- Check for skipped stage ---
    for rl in raw_lines:
        if _SKIPPED_RE.search(rl):
            stage.status = "skipped"
            stage.lines = []
            output.append(stage)
            return

    # --- Filter webhook noise and deduplicate ---
    filtered: List[str] = [l for l in raw_lines if not _is_webhook_noise(l)]
    has_docker = _stage_has_docker_content(filtered)
    filtered = _deduplicate_lines(filtered, skip_docker=has_docker)

    # --- Detect failure ---
    has_failure = False
    for fl in filtered:
        if _has_failure_pattern(fl.rstrip()):
            has_failure = True
            break
    if has_failure:
        stage.status = "failed"

    # --- Parse lines ---
    if stage.name in _BUILD_LOG_STAGES or stage.name in _INFRA_APPLY_LOG_STAGES:
        # Build / infra-apply stages get full log output (Docker or generic)
        if _stage_has_docker_content(filtered):
            stage.lines = _parse_docker_lines(filtered)
        else:
            stage.lines = _parse_generic_lines(filtered)
    else:
        # All other stages get friendly human-readable summaries
        stage.lines = _parse_summary_lines(stage.name, filtered)
        # If the stage failed, append the actual error line for context
        if has_failure:
            _append_error_context(stage.lines, filtered)

    # --- Skip wrapper stages that contain no meaningful content ---
    if stage.name in _WRAPPER_STAGE_NAMES and not stage.lines:
        return

    output.append(stage)


def _parse_summary_lines(stage_name: str, raw_lines: List[str]) -> List[LogLine]:
    """
    Generate friendly human-readable summary lines for non-Build stages.

    Instead of showing raw shell commands and their output, we scan the raw
    lines for key actions and produce short, user-friendly descriptions.
    """
    summaries: List[LogLine] = []
    joined = "\n".join(raw_lines)

    stage_lower = stage_name.lower()

    # --- Model Download (model-serving pipelines) ---
    # Stage name example: "Model Download"
    if "model download" in stage_lower:
        model_match = _MODEL_ID_RE.search(joined) or _HF_DOWNLOAD_RE.search(joined)
        if model_match and model_match.lastindex and model_match.group(1):
            summaries.append(LogLine(
                message=f"Downloading model: {model_match.group(1)}",
                status="done",
            ))
        else:
            summaries.append(LogLine(message="Downloading model weights", status="done"))
        return summaries

    # --- Checkout / Cloning Repo (infra-apply stage uses "Cloning Repo") ---
    if "checkout" in stage_lower or "cloning" in stage_lower:
        # Try old-style git clone first, then new-style git fetch + remote
        m = _GIT_CLONE_RE.search(joined)
        if m:
            branch, repo = m.group(1), m.group(2)
            summaries.append(LogLine(message=f"Cloning {repo} (branch: {branch})", status="done"))
        else:
            repo_match = _GIT_REMOTE_RE.search(joined)
            branch_match = _GIT_FETCH_RE.search(joined)
            repo = repo_match.group(1) if repo_match else None
            branch = branch_match.group(1) if branch_match else None
            if repo and branch:
                summaries.append(LogLine(message=f"Cloning {repo} (branch: {branch})", status="done"))
            elif repo:
                summaries.append(LogLine(message=f"Cloning {repo}", status="done"))
            else:
                summaries.append(LogLine(message="Cloning repository", status="done"))
        return summaries

    # --- Capturing Outputs (infra-apply) ---
    # Stage just runs `terragrunt output -json` — surface a one-liner instead
    # of the workdir + bare command line.
    if "capturing" in stage_lower:
        summaries.append(LogLine(message="Captured Terraform outputs", status="done"))
        return summaries

    # --- Terragrunt Init (infra-apply) ---
    # 24 lines of provider download / lock-file boilerplate that nobody reads
    # on a healthy run. Surface a one-liner; if the stage failed,
    # `_append_error_context` will tack on the actual error after.
    if "terragrunt init" in stage_lower:
        provider_match = re.search(r"Installed\s+hashicorp/aws\s+v(\S+)", joined)
        if provider_match:
            summaries.append(LogLine(
                message=f"Terraform initialized (aws provider v{provider_match.group(1)})",
                status="done",
            ))
        else:
            summaries.append(LogLine(message="Terraform initialized", status="done"))
        return summaries

    # --- Initialisation ---
    if "initiali" in stage_lower:
        # K8s manifests
        manifests = _KUBECTL_APPLY_RE.findall(joined)
        if manifests:
            summaries.append(LogLine(message="Provisioning Kubernetes infrastructure", status="done"))
        # ECR
        ecr_match = _ECR_REPO_RE.search(joined)
        if ecr_match:
            summaries.append(LogLine(
                message=f"Configuring ECR repository ({ecr_match.group(1)})",
                status="done",
            ))
        if not summaries:
            summaries.append(LogLine(message="Setting up infrastructure", status="done"))
        return summaries

    # --- Helm: Building stage (packages the chart, no Docker build) ---
    # Helm pipelines use "Building" as the stage name where values.yaml /
    # helm templates are rendered. There's no docker build — this is just
    # chart preparation.
    if stage_lower == "building":
        summaries.append(LogLine(message="Preparing Helm chart", status="done"))
        return summaries

    # --- Deploy / Deploying (covers "deploy" in service pipelines AND
    #     "Deploying" in helm pipelines via the `deploy` substring) ---
    if "deploy" in stage_lower:
        helm_match = _HELM_INSTALL_RE.search(joined)
        if helm_match:
            summaries.append(LogLine(
                message=f"Installing Helm release: {helm_match.group(1)}",
                status="done",
            ))
            return summaries
        # Docker push (one summary regardless of how many pushes)
        if _DOCKER_PUSH_RE.search(joined):
            summaries.append(LogLine(message="Pushing Docker image to registry", status="done"))
        # Deployment rollout
        rollout_match = _ROLLOUT_RE.search(joined)
        if rollout_match:
            svc = rollout_match.group(1)
            summaries.append(LogLine(message=f"Deploying {svc}", status="done"))
        elif "kubectl apply" in joined:
            summaries.append(LogLine(message="Applying deployment", status="done"))
        if not summaries:
            summaries.append(LogLine(message="Deploying service", status="done"))
        return summaries

    # --- Verify ---
    if "verif" in stage_lower:
        # Infra-apply permission verification (stage name "Verifying Permissions").
        # Hide the verbose `aws iam simulate-principal-policy` command — users only
        # need to know whether perms passed or not; details surface in error context
        # if it failed.
        if "permission" in stage_lower:
            had_error = any(_has_failure_pattern(l.rstrip()) for l in raw_lines)
            if had_error:
                summaries.append(LogLine(message="Permissions check failed", status="failed"))
            else:
                summaries.append(LogLine(message="Permissions verified", status="done"))
            return summaries
        alb_match = _ALB_HOSTNAME_RE.search(joined)
        if alb_match:
            summaries.append(LogLine(message="Verifying service is running", status="done"))
            summaries.append(LogLine(message="URL provisioned", status="done"))
        else:
            summaries.append(LogLine(message="Verifying service is running", status="done"))
            summaries.append(LogLine(message="Waiting for URL to be provisioned", status="running"))
        return summaries

    # --- Fallback: unknown stage — show a generic summary ---
    summaries.append(LogLine(message=f"Running {stage_name}", status="done"))
    return summaries


def _append_error_context(summaries: List[LogLine], raw_lines: List[str]) -> None:
    """If there's a failure, append the error line to the summaries so the user can see what went wrong."""
    for raw in raw_lines:
        line = raw.rstrip()
        if line and _has_failure_pattern(line):
            summaries.append(LogLine(message=line, status="failed"))
            break


def _parse_generic_lines(raw_lines: List[str]) -> List[LogLine]:
    """
    Parse non-Docker stage output into LogLine entries.

    Shell commands (``+ cmd``) get ``status="command"`` with the command
    portion extracted.  Other non-blank lines become plain LogLine entries.

    Terragrunt-specific cleanup:
    - Strips the ``HH:MM:SS.sss STDOUT terraform:`` prefix and lifts the
      timestamp into ``LogLine.timestamp``.
    - Tags ``Plan: ...`` / ``Apply complete! ...`` lines with ``status="summary"``
      so the UI can render them as a banner instead of a flat line.
    - Tags ``Warning: ...`` blocks (and their box-drawing continuation lines)
      with ``status="warning"`` so the UI can amber-highlight them.
    """
    parsed: List[LogLine] = []
    in_warning_block = False
    for raw in raw_lines:
        line = raw.rstrip()
        if not line:
            in_warning_block = False
            continue

        m = _SHELL_CMD_RE.match(line)
        if m:
            cmd_text = m.group(1).strip()
            parsed.append(LogLine(message=cmd_text, command=cmd_text, status="command"))
            in_warning_block = False
            continue

        # Lift Terragrunt timestamp prefix
        timestamp: Optional[str] = None
        tf_match = _TF_PREFIX_RE.match(line)
        if tf_match:
            timestamp = tf_match.group(1)
            line = line[tf_match.end():]
            if not line:
                continue

        # Plan / Apply summary banners
        if _TF_PLAN_RE.match(line) or _TF_APPLY_RE.match(line):
            parsed.append(LogLine(message=line, timestamp=timestamp, status="summary"))
            in_warning_block = False
            continue

        # Warning header opens a multi-line block bounded by ╷ ... ╵.
        # The opener (╷) lands before the Warning: line, so retro-tag it.
        if _TF_WARNING_HEADER_RE.match(line):
            if parsed and parsed[-1].status is None and parsed[-1].message.strip() == "╷":
                parsed[-1].status = "warning"
            parsed.append(LogLine(message=line, timestamp=timestamp, status="warning"))
            in_warning_block = True
            continue
        if in_warning_block and _TF_BLOCK_GUTTER_RE.match(line):
            parsed.append(LogLine(message=line, timestamp=timestamp, status="warning"))
            # ╵ closes the block
            if line.startswith("╵"):
                in_warning_block = False
            continue

        parsed.append(LogLine(message=line, timestamp=timestamp))

    return parsed

"""
PR Workflow Context

Simple context object for managing workflow state.
Passed by reference to handlers which update the dictionaries.
"""

from typing import Dict, Any, List
from collections import defaultdict
from app.schemas.file_location_response_schema import FileLocationResponse

class PRWorkflowContext:
    """
    Context for PR workflow orchestration.

    Simple dictionary-based state management:
    - feature_branches: {"{repo}|||{base_branch}": "feature_branch_name"}
    - file_location_responses: {queue_id: response}
    - script_gen_responses: {queue_id: {script_gen_key: {"original_content": "string", "preview_contents": "string"}}}
    - gitops_responses: {"{repo}|||{base_branch}": {"commit": {...}, "pr": {...}}}

    Passed by reference to handlers, which update dictionaries directly.

    Usage:
        # Create context once in service
        workflow_context = PRWorkflowContext()

        # Handlers update context directly
        file_locator.locate(tenant, queue_dict, workflow_context)
        await script_gen.generate_script(tenant, queue_dict, response, workflow_context)
        gitops_handler.create_commit(tenant, ..., workflow_context)
        gitops_handler.create_pr(tenant, ..., workflow_context)

        # Get final state
        result = workflow_context.to_dict()
    """

    def __init__(self, skip_commit: bool = False, is_conflict_resolve: bool = False):
        """
        Initialize empty PR workflow context.

        Args:
            skip_commit: If True, skip Git commits (used for preview mode)
            is_conflict_resolve: If True, reuse pre-populated feature branches instead of creating new ones
        """
        # Feature branches: {"{repo}|||{base_branch}": {"branch": str, "type": str}}
        self.feature_branches: Dict[str, Any] = {}

        # File location responses: {queue_id: response}
        self.file_location_responses: Dict[int, FileLocationResponse] = {}

        # Config snapshots: {queue_id: config_snapshot_dict}
        self.config_snapshots: Dict[int, Dict[str, Any]] = {}

        # Queue metadata: {queue_code: {"transaction_code": str, "table_name": enum}}
        # Used for linking gitops_workflow_detail to source entities
        self.queue_metadata: Dict[str, Dict[str, Any]] = {}

        # Script generation responses: {queue_id: {script_gen_key: {"original_content": "string", "preview_contents": "string"}}}
        self.script_gen_responses: Dict[int, Dict[str, Dict[str, str]]] = defaultdict(lambda: defaultdict(dict))

        # GitOps responses: {"{repo}-{base_branch}": {"commit": {...}, "pr": {...}}}
        self.gitops_responses: Dict[str, Dict[str, Any]] = {}

        # Staged files for batched commits (list of dicts keyed by repo/branch/path)
        # Each entry: {repo, base_branch, feature_branch, file_path, content, queue_id, script_gen_key, mode}
        self.staged_files: List[Dict[str, Any]] = []

        # kubectl apply results: [{job_name, namespace, status, error?}]
        self.kubectl_results: List[Dict[str, Any]] = []

        # Commit messages per repo/branch: {"{repo}|||{base_branch}": "line1\nline2"}
        self.commit_messages: Dict[str, str] = {}

        # Dockerfile workflow codes by repo/branch: {"{repo}|||{base_branch}": {"SCDF_xxx", ...}}
        self.dockerfile_workflow_codes: Dict[str, set] = defaultdict(set)

        # Pipeline codes by repo/branch: {"{repo}|||{base_branch}": {"pipeline_xxx", ...}}
        self.pipeline_codes: Dict[str, set] = defaultdict(set)

        # Skip commits flag (for preview mode)
        self.skip_commit: bool = skip_commit

        # Conflict resolve flag (reuse existing feature branches, skip new branch creation)
        self.is_conflict_resolve: bool = is_conflict_resolve

        # Queue-status decisions, keyed by queue_code. Producers (PR creation,
        # no-diff handling, conflict-resolve) write their intended final state
        # here; a single applier at the end of the workflow flushes them in
        # bulk. Value shapes:
        #   {"status": TransactionQueueStatusEnum.PR_RAISED}  → bulk_update_status_by_codes
        #   {"is_deleted": True}                              → bulk_soft_delete_by_codes
        #   {"status": ..., "is_deleted": True}               → both (rare)
        # Last-write-wins per queue_code.
        self.queue_status_updates: Dict[str, Dict[str, Any]] = {}

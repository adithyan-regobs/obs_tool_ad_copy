"""
VariableDeployWorkflow — child of DeploymentOrchestratorWorkflow.

Deploys the caller's staged variables (secrets → Secrets Manager, configs →
SSM) for ONE service via a single activity. Holds NO locks: the orchestrator
parent acquires the project_dirs before starting this child and releases them
in its finally — this workflow only does the work.

Input/output are codes and statuses only — variable VALUES never enter
Temporal payloads (they are read from the staged bucket inside the activity).
"""

from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.temporal.activities.multiple_deploy_activities import deploy_variables_activity
    from app.temporal.activities.deploy_activities import update_pipeline_run_track_stage
    from app.temporal.error_utils import error_message

# Single attempt: a retry re-runs the WHOLE activity (all SM/SSM writes again,
# ~minutes) even when only the final DB step failed. Fail fast instead — the
# staged file survives, so the user can redeploy after fixing the cause.
_RETRY = RetryPolicy(maximum_attempts=1)


@workflow.defn
class VariableDeployWorkflow:

    def __init__(self):
        self._step: str = "starting"

    @workflow.run
    async def run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        params: {transaction_code, table_name, environment,
                 user_code, tenant_code, track_id}
        Returns: {"all_success": bool, "results": [...], "error": str | None}
        """
        track_id = params["track_id"]
        act_opts_stage = dict(
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )

        # No wrapper stage: the activity's milestone callback writes the real
        # stages ('secrets: save' / 'configs: save'). Only a failure that
        # the activity could not record itself (dead before any milestone)
        # needs a closure here — written on 'completed' so the row can never
        # hang RUNNING / stay green after a variables failure.
        self._step = "deploying_variables"
        try:
            result = await workflow.execute_activity(
                deploy_variables_activity,
                args=[params],
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=_RETRY,
            )
        except Exception as exc:
            self._step = "failed"
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[track_id, "completed", "failed", workflow.now().isoformat(), error_message(exc)],
                **act_opts_stage,
            )
            return {"all_success": False, "results": [], "error": error_message(exc)}

        all_success = result.get("all_success")
        self._step = "completed" if all_success else "failed"
        if not all_success:
            await workflow.execute_activity(
                update_pipeline_run_track_stage,
                args=[
                    track_id,
                    "completed",
                    "failed",
                    workflow.now().isoformat(),
                    result.get("error") or "one or more variables failed",
                ],
                **act_opts_stage,
            )
        return result

    @workflow.query
    def get_state(self) -> dict:
        return {"step": self._step}
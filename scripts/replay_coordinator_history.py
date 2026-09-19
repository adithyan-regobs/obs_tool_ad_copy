"""
Replay a saved coordinator history against the CURRENT workflow code.

This is the pre-deploy safety check for any change to
TenantCoordinatorWorkflow: a change that replays cleanly here cannot wedge
the running coordinators, and one that does not will wedge them on the next
signal they receive.

Get a history:
    curl -s '<base>/api/v1/temporal/workflows/coordinator-aspora/history' \
        > /tmp/coordinator-aspora.json

Run:
    python scripts/replay_coordinator_history.py /tmp/coordinator-aspora.json [...]

Exit code 0 = every history replayed. Non-zero = at least one would break.
Check BOTH vintages before releasing:
  - a pre-2026-09-02 run (prod coordinator-aspora / coordinator-vance)
  - a run born of continue-as-new (stage coordinator-aspora)
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from app.temporal.workflows.coordinator_workflow import TenantCoordinatorWorkflow


async def replay_one(path: str) -> bool:
    with open(path) as fh:
        raw = json.load(fh)

    history = WorkflowHistory.from_json("replay", raw)
    events = raw.get("events") or []
    print(f"\n{path}\n  run_id={history.run_id} events={len(events)}")

    replayer = Replayer(workflows=[TenantCoordinatorWorkflow])
    try:
        await replayer.replay_workflow(history)
    except Exception as exc:
        print(f"  FAILED — this history would wedge the coordinator:\n    {exc}")
        return False

    print("  OK — replays to the end of history")
    return True


async def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        return 2

    results = [await replay_one(p) for p in paths]
    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} replayed cleanly")
    if not ok:
        print("DO NOT DEPLOY — fix the divergence first.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

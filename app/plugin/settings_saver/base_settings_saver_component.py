"""
Settings Saver Component Interface

Contract for writing an APPROVED queue item's `config_snapshot` into whatever
live table that kind of change describes — service_configs for a service's
settings, infra_mst for a bucket, a queue or a table, and so on.

Why this exists: Save deliberately writes nothing live. It parks the proposed
configuration in `config_snapshot` and leaves the live row describing what is
really running, which is what makes that row an honest baseline for the next
diff. When a change actually ships, something has to close that gap — otherwise
the settings screen keeps showing the old values however many times the change
deploys. This family is that something.

One component per KIND of change, dispatched on `case_ref_code`, because the
queue is polymorphic: transaction_code names a service_configs row here and an
infra_mst row there, and the table to write differs with it. Branching inside
one saver would grow a conditional for every resource type the product adds.

Adding a resource type: drop a file in this folder, subclass this, implement
`run`, and register the class in ``ResourceSettingsSaverHandler._build_map()``.
Nothing else changes — the handler already runs for every queue item and skips
the ones with no component registered.

Error convention: `run` may raise. The handler catches, logs and carries on, so
one stale live row never fails a deploy that has already happened — the PR is
raised by then and the change is out. A raise means "this needs a human", not
"roll it back".
"""

from abc import ABC, abstractmethod
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession


class BaseSettingsSaverComponent(ABC):
    """Writes one kind of approved change into the live table it describes."""

    @abstractmethod
    async def run(
        self,
        tenant: str,
        queue_dict: dict,
        db: Optional[AsyncSession] = None,
    ) -> bool:
        """Apply this queue item's approved snapshot to its live record.

        `queue_dict` is the same shape the pre-action and post-action handlers
        receive — id, code, tenant_code, transaction_code, case_ref_code,
        table_name, config_snapshot, environment, geo_loc_mst_code — so a
        component needs no second query to do its work.

        Deliberately no `workflow_context`: post-actions take one because they
        read what script generation produced, while saving settings depends only
        on the approved record and the live row it targets.

        Returns True when something was written, False when there was
        legitimately nothing to do — the target was deleted after approval, or
        the snapshot carries no fields this component owns. False is not a
        failure; raise for that.

        MUST NOT commit. The handler owns the transaction so one deploy's writes
        land or roll back together.
        """
        raise NotImplementedError

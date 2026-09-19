"""
Aspora Resource Pre-Action Component

Aspora and Vance own their own resource lifecycle UX through the canvas, so
they intentionally run no pre-action — the canvas drives status transitions
itself rather than relying on the early `INITIALISING` flip the default
component performs.

This component is a deliberate no-op. Replace its body when Aspora/Vance
need real pre-action behavior (e.g. seeding k8s-manifests state before
script-gen).
"""

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession


class AsporaResourcePreActionComponent:

    def __init__(self):
        self.logger = logging.getLogger(__name__)

    async def run(
        self,
        tenant: str,
        queue_dict: dict,
        db: Optional[AsyncSession] = None,
    ) -> None:
        # No-op: Aspora/Vance do not flip resource status here.
        return

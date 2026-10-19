"""Procedural operations: deterministic code that acts on a finished request. No model calls them.

Following docs/procedural-ops-credentials.md, these services are handed only to the graph's
deterministic nodes, never put in the run context that model-calling nodes see, and never wrapped
as tools. Each one will later call the ops-executor; for now Spec is ready only logs.
"""

import logging
from datetime import UTC, datetime

from app.run_request import SpecReadyReceipt

logger = logging.getLogger("app.ops.spec_ready")


class SpecReadyService:
    """Hand-off for a run request whose dataset list the person has marked ready. Stub: logs it."""

    async def spec_ready(self, request_id: str, datasets: list[str]) -> SpecReadyReceipt:
        logger.info("Spec is ready for run request %s: %d datasets", request_id, len(datasets))
        for name in datasets:
            logger.info("  %s", name)
        return SpecReadyReceipt(request_id=request_id, datasets=len(datasets), accepted_at=datetime.now(UTC))

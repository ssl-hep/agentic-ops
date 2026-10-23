"""The phase 0 no-op workflow: one probe_env activity on a worker queue."""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@dataclass
class ProbeRequest:
    task_queue: str
    # How long the activity heartbeats before it returns
    work_seconds: int = 30
    # How long to wait for a worker to pick the task up and finish it (AF jobs can queue)
    wait_hours: float = 6.0


@workflow.defn
class NoopWorkflow:
    @workflow.run
    async def run(self, req: ProbeRequest) -> dict:
        # By name: the worker side does not import workflow code and vice versa
        return await workflow.execute_activity(
            "probe_env",
            req,
            task_queue=req.task_queue,
            schedule_to_close_timeout=timedelta(hours=req.wait_hours),
            start_to_close_timeout=timedelta(seconds=req.work_seconds) + timedelta(minutes=10),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

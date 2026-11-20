"""Trivial workflows that probe what a test environment supports (tests/test_environments.py).

Kept out of the test modules: the workflow sandbox re-imports a workflow's module, and these
should import nothing but temporalio and the package.
"""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributePair, TypedSearchAttributes
from temporalio.exceptions import ApplicationError

from agentic_ops_worker import search_attributes as sa

ECHO = "probe_echo"


@dataclass
class EchoInput:
    queue: str
    text: str
    # A durable timer before the activity, to see whether the environment skips time. Seconds:
    # the default converter cannot encode a timedelta.
    sleep_seconds: float = 0


@workflow.defn
class EchoWorkflow:
    """Sleeps, then runs probe_echo by name on another queue, as workflows call EWKit's activities."""

    @workflow.run
    async def run(self, inp: EchoInput) -> str:
        if inp.sleep_seconds:
            await workflow.sleep(inp.sleep_seconds)
        return await workflow.execute_activity(
            ECHO,
            inp.text,
            task_queue=inp.queue,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
            result_type=str,
        )


@dataclass
class Decision:
    action: str
    memory_mb: int = 0


@workflow.defn
class DecideWorkflow:
    """Waits for a decide Update, whose validator rejects actions outside a closed set."""

    def __init__(self) -> None:
        self.decision: Decision | None = None

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self.decision is not None)
        assert self.decision
        return self.decision.action

    @workflow.update
    async def decide(self, decision: Decision) -> str:
        self.decision = decision
        return f"applied {decision.action}"

    @decide.validator
    def check_decision(self, decision: Decision) -> None:
        if decision.action not in ("retry", "retry_with_memory", "escalate"):
            raise ApplicationError(f"action {decision.action!r} is not allowed")
        if decision.memory_mb > 16384:
            raise ApplicationError(f"memory {decision.memory_mb} MB is above the cap")


@workflow.defn
class ParentWorkflow:
    """Starts one child per ID, as a production starts its datasets, and sums their results."""

    @workflow.run
    async def run(self, child_ids: list[str]) -> list[str]:
        results = []
        for child_id in child_ids:
            results.append(await workflow.execute_child_workflow(ChildWorkflow.run, child_id, id=child_id))
        return results


@workflow.defn
class ChildWorkflow:
    @workflow.run
    async def run(self, child_id: str) -> str:
        return f"{workflow.info().parent.workflow_id if workflow.info().parent else None} -> {child_id}"


@workflow.defn
class UpsertWorkflow:
    """Upserts the dataset search attributes, then waits to be told to finish."""

    def __init__(self) -> None:
        self.finish = False

    @workflow.run
    async def run(self, production_id: str) -> None:
        workflow.upsert_search_attributes(
            [
                sa.PRODUCTION_ID.value_set(production_id),
                sa.DATASET_STATE.value_set("RUNNING"),
                sa.UNITS_DONE.value_set(3),
                sa.UNITS_TOTAL.value_set(10),
                sa.NEEDS_DECISION.value_set(True),
            ]
        )
        await workflow.wait_condition(lambda: self.finish)

    @workflow.signal
    def done(self) -> None:
        self.finish = True


def expected_search_attributes(production_id: str) -> TypedSearchAttributes:
    return TypedSearchAttributes(
        [
            SearchAttributePair(sa.PRODUCTION_ID, production_id),
            SearchAttributePair(sa.DATASET_STATE, "RUNNING"),
            SearchAttributePair(sa.UNITS_DONE, 3),
            SearchAttributePair(sa.UNITS_TOTAL, 10),
            SearchAttributePair(sa.NEEDS_DECISION, True),
        ]
    )

"""What the two test environments support, checked with trivial workflows (phase 2 step 1).

Both run activities by name on another queue, Updates with validators, child workflows with
deterministic IDs, and search attribute upserts (once registered; see conftest.py). Only the
time-skipping server skips timers. Only the local dev server answers visibility queries
(ListWorkflowExecutions) and task queue statistics (DescribeTaskQueue), so tests of the pool
keeper's backlog reads and of listing by search attribute run on the local server.

Where an environment lacks a feature, the test asserts the failure, so a change in either server
shows up here.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta

import pytest
from probe_workflows import (
    ECHO,
    ChildWorkflow,
    DecideWorkflow,
    Decision,
    EchoInput,
    EchoWorkflow,
    ParentWorkflow,
    UpsertWorkflow,
    expected_search_attributes,
)
from temporalio import activity
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client, WorkflowUpdateFailedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agentic_ops_worker import names
from agentic_ops_worker import search_attributes as sa

WORKFLOWS = [EchoWorkflow, DecideWorkflow, ParentWorkflow, ChildWorkflow, UpsertWorkflow]


@activity.defn(name=ECHO)
async def echo(text: str) -> str:
    return f"echo {text}"


def new_production() -> str:
    return f"probe{uuid.uuid4().hex[:8]}"


@pytest.fixture
def production() -> str:
    return new_production()


@pytest.fixture
async def client(env: WorkflowEnvironment, production: str) -> AsyncIterator[Client]:
    """The environment's client, with a controller worker for the probe workflows and an
    activity worker on the production's 4g unit queue."""
    async with (
        Worker(env.client, task_queue=names.CONTROLLER_QUEUE, workflows=WORKFLOWS),
        Worker(env.client, task_queue=names.unit_queue(production, "4g"), activities=[echo]),
    ):
        yield env.client


async def eventually[T](check: Callable[[], Awaitable[T]], timeout: float = 10) -> T:
    """Retry check until it stops raising AssertionError: visibility and task queue statistics
    are updated asynchronously."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return await check()
        except AssertionError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.2)


async def assert_unimplemented(call: Awaitable[object]) -> None:
    with pytest.raises(RPCError) as err:
        await call
    assert err.value.status == RPCStatusCode.UNIMPLEMENTED, err.value


async def test_activity_by_name_on_unit_queue(client: Client, production: str) -> None:
    result = await client.execute_workflow(
        EchoWorkflow.run,
        EchoInput(queue=names.unit_queue(production, "4g"), text="hello"),
        id=names.production_workflow_id(production),
        task_queue=names.CONTROLLER_QUEUE,
    )
    assert result == "echo hello"


async def test_timers(env: WorkflowEnvironment, client: Client, production: str) -> None:
    """A day-long timer: skipped under time skipping, real time on the local server."""
    inp = EchoInput(names.unit_queue(production, "4g"), "later", sleep_seconds=timedelta(days=1).total_seconds())
    handle = await client.start_workflow(
        EchoWorkflow.run, inp, id=names.production_workflow_id(production), task_queue=names.CONTROLLER_QUEUE
    )
    if env.supports_time_skipping:
        assert await asyncio.wait_for(handle.result(), timeout=10) == "echo later"
    else:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(handle.result()), timeout=2)
        await handle.terminate()


async def test_update_with_validator(client: Client, production: str) -> None:
    handle = await client.start_workflow(
        DecideWorkflow.run, id=names.production_workflow_id(production), task_queue=names.CONTROLLER_QUEUE
    )
    with pytest.raises(WorkflowUpdateFailedError) as rejected:
        await handle.execute_update(DecideWorkflow.decide, Decision("exclude_files"))
    assert "not allowed" in str(rejected.value.cause)
    with pytest.raises(WorkflowUpdateFailedError) as rejected:
        await handle.execute_update(DecideWorkflow.decide, Decision("retry_with_memory", memory_mb=65536))
    assert "above the cap" in str(rejected.value.cause)
    applied = await handle.execute_update(DecideWorkflow.decide, Decision("retry_with_memory", memory_mb=8192))
    assert applied == "applied retry_with_memory"
    assert await handle.result() == "retry_with_memory"


async def test_child_workflows(client: Client, production: str) -> None:
    parent_id = names.production_workflow_id(production)
    child_ids = [
        names.dataset_workflow_id(production, f"mc23_13p6TeV:mc23_13p6TeV.60118{i}.DAOD_PHYS") for i in range(3)
    ]
    result = await client.execute_workflow(
        ParentWorkflow.run, child_ids, id=parent_id, task_queue=names.CONTROLLER_QUEUE
    )
    assert result == [f"{parent_id} -> {child_id}" for child_id in child_ids]


async def test_search_attributes(env: WorkflowEnvironment, client: Client, production: str) -> None:
    handle = await client.start_workflow(
        UpsertWorkflow.run,
        production,
        id=names.dataset_workflow_id(production, "mc23_13p6TeV:upsert"),
        task_queue=names.CONTROLLER_QUEUE,
    )

    async def upserted() -> None:
        attributes = (await handle.describe()).typed_search_attributes
        expected = expected_search_attributes(production)
        assert {key.name: attributes.get(key) for key in sa.ALL} == {key.name: expected.get(key) for key in sa.ALL}

    async def listed() -> None:
        query = f'ProductionId = "{production}" AND NeedsDecision = true'
        assert [w.id async for w in client.list_workflows(query)] == [handle.id]

    await eventually(upserted)
    if env.supports_time_skipping:
        await assert_unimplemented(listed())
    else:
        await eventually(listed)
    await handle.signal(UpsertWorkflow.done)
    await handle.result()


async def test_task_queue_backlog(env: WorkflowEnvironment, client: Client) -> None:
    """The pool keeper reads each unit queue's backlog: one activity waiting on a queue nobody polls."""
    queue = names.unit_queue(new_production(), "8g")
    handle = await client.start_workflow(
        EchoWorkflow.run,
        EchoInput(queue=queue, text="waiting"),
        id=names.production_workflow_id(new_production()),
        task_queue=names.CONTROLLER_QUEUE,
    )

    async def backlog() -> int:
        response = await client.workflow_service.describe_task_queue(
            DescribeTaskQueueRequest(
                namespace=client.namespace,
                task_queue=TaskQueue(name=queue),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
                report_stats=True,
            )
        )
        assert response.stats.approximate_backlog_count == 1
        return response.stats.approximate_backlog_count

    if env.supports_time_skipping:
        await assert_unimplemented(backlog())
    else:
        assert await eventually(backlog) == 1
    await handle.terminate()

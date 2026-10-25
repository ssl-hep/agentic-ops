"""Question 8 against a running server: a worker certificate cannot start or cancel workflows.

    uv run pytest                       # local server from local/compose.yaml
    PHASE0_ADDRESS=host:443 PHASE0_SERVER_NAME=host uv run pytest   # River
    PHASE0_ADDRESS=<LB IP>:7233 PHASE0_SERVER_NAME=<name> PHASE0_NAMESPACE=ewkit-production \
        PHASE0_CERTS=secrets/temporal-pki/test uv run pytest        # phase 1 (charts/temporal)

Certificates come from PHASE0_CERTS (default secrets/phase0-certs), as written by
`agentic-ops-pki dev` (pki/). The namespace must be registered first (python -m shim.admin). Each test opens its own connections: the SDK's
clients belong to the event loop that made them.
"""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.api.common.v1 import Payloads, WorkflowExecution
from temporalio.api.enums.v1 import TaskQueueKind
from temporalio.api.operatorservice.v1 import ListSearchAttributesRequest
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import (
    GetWorkflowExecutionHistoryRequest,
    PollActivityTaskQueueRequest,
    PollWorkflowTaskQueueRequest,
    RespondActivityTaskCompletedByIdRequest,
    RespondActivityTaskCompletedRequest,
    RegisterNamespaceRequest,
)
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from shim.common import AF_IO_QUEUE, CONTROLLER_QUEUE, NAMESPACE, UNIT_QUEUE, certs_root, connect
from shim.noop_worker import probe_env
from shim.workflows import NoopWorkflow, ProbeRequest

ADDRESS = os.environ.get("PHASE0_ADDRESS", "localhost:7233")
SERVER_NAME = os.environ.get("PHASE0_SERVER_NAME", ADDRESS.rsplit(":", 1)[0])
SHORT = timedelta(seconds=3)


async def client_for(name: str, **kwargs) -> Client:
    return await connect(ADDRESS, certs_root() / "clients" / name, SERVER_NAME, identity=f"pytest-{name}", **kwargs)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module", autouse=True)
def server_reachable():
    try:
        run(client_for("controller"))
    except Exception as e:  # noqa: BLE001 - any failure here means there is nothing to test against
        pytest.skip(f"no phase 0 server at {ADDRESS}: {e}")


async def start_sleeping_workflow(controller: Client) -> str:
    """A workflow nobody runs: it stays open on a queue no worker polls."""
    workflow_id = f"dataset/phase0/authz-{uuid.uuid4()}"
    await controller.start_workflow("Sleeping", id=workflow_id, task_queue="nobody-polls-this")
    return workflow_id


async def assert_denied(call) -> None:
    with pytest.raises(RPCError) as err:
        await call
    assert err.value.status == RPCStatusCode.PERMISSION_DENIED, err.value


def poll_activity(queue: str) -> PollActivityTaskQueueRequest:
    return PollActivityTaskQueueRequest(namespace=NAMESPACE, task_queue=TaskQueue(name=queue, kind=TaskQueueKind.TASK_QUEUE_KIND_NORMAL), identity="pytest")


async def poll_is_allowed(client: Client, queue: str) -> None:
    """A long poll on an empty queue ends in a timeout, not a permission error."""
    try:
        await client.workflow_service.poll_activity_task_queue(poll_activity(queue), timeout=SHORT)
    except RPCError as e:
        assert e.status in (RPCStatusCode.DEADLINE_EXCEEDED, RPCStatusCode.CANCELLED), e


def test_controller_starts_and_terminates_workflows():
    async def go():
        controller = await client_for("controller")
        workflow_id = await start_sleeping_workflow(controller)
        await controller.get_workflow_handle(workflow_id).terminate("authz test")

    run(go())


def test_worker_cannot_touch_workflows():
    async def go():
        controller = await client_for("controller")
        worker = await client_for("ewkit-phase0")
        workflow_id = await start_sleeping_workflow(controller)
        handle = worker.get_workflow_handle(workflow_id)
        try:
            await assert_denied(worker.start_workflow("Sleeping", id=f"{workflow_id}-2", task_queue="x"))
            await assert_denied(handle.signal("anything"))
            await assert_denied(handle.cancel())
            await assert_denied(handle.terminate("worker says so"))
            await assert_denied(handle.describe())
            await assert_denied(worker.count_workflows())
            await assert_denied(
                worker.workflow_service.get_workflow_execution_history(
                    GetWorkflowExecutionHistoryRequest(namespace=NAMESPACE, execution=WorkflowExecution(workflow_id=workflow_id))
                )
            )
            await assert_denied(
                worker.workflow_service.poll_workflow_task_queue(
                    PollWorkflowTaskQueueRequest(namespace=NAMESPACE, task_queue=TaskQueue(name=CONTROLLER_QUEUE)),
                    timeout=SHORT,
                )
            )
            await assert_denied(
                worker.workflow_service.respond_activity_task_completed_by_id(
                    RespondActivityTaskCompletedByIdRequest(namespace=NAMESPACE, workflow_id=workflow_id, activity_id="1")
                )
            )
            await assert_denied(
                worker.operator_service.list_search_attributes(ListSearchAttributesRequest(namespace=NAMESPACE))
            )
        finally:
            await controller.get_workflow_handle(workflow_id).terminate("authz test")

    run(go())


def test_worker_polls_only_its_own_queues():
    async def go():
        unit = await client_for("ewkit-phase0")
        af_io = await client_for("af-io")
        await poll_is_allowed(unit, UNIT_QUEUE)
        await poll_is_allowed(unit, "ewkit-phase0-16g")
        await assert_denied(unit.workflow_service.poll_activity_task_queue(poll_activity("ewkit-phase1-default"), timeout=SHORT))
        await assert_denied(unit.workflow_service.poll_activity_task_queue(poll_activity(AF_IO_QUEUE), timeout=SHORT))
        await poll_is_allowed(af_io, AF_IO_QUEUE)
        await assert_denied(af_io.workflow_service.poll_activity_task_queue(poll_activity(UNIT_QUEUE), timeout=SHORT))

    run(go())


def test_task_token_of_another_production_is_rejected():
    """Production phase1's worker cannot complete a task that phase0's worker received."""

    async def go():
        controller = await client_for("controller")
        phase0 = await client_for("ewkit-phase0")
        phase1 = await client_for("ewkit-phase1")
        workflow_id = f"dataset/phase0/authz-token-{uuid.uuid4()}"
        async with Worker(controller, task_queue=CONTROLLER_QUEUE, workflows=[NoopWorkflow]):
            handle = await controller.start_workflow(
                NoopWorkflow.run, ProbeRequest(task_queue=UNIT_QUEUE, work_seconds=0, wait_hours=0.1),
                id=workflow_id, task_queue=CONTROLLER_QUEUE,
            )
            task = await phase0.workflow_service.poll_activity_task_queue(poll_activity(UNIT_QUEUE), timeout=timedelta(seconds=30))
            assert task.task_token, "no activity task arrived"
            assert task.workflow_execution.workflow_id == workflow_id
            stolen = RespondActivityTaskCompletedRequest(namespace=NAMESPACE, task_token=task.task_token, identity="phase1")
            await assert_denied(phase1.workflow_service.respond_activity_task_completed(stolen))
            # The rightful worker can still finish it
            payload = await phase0.data_converter.encode([{"by": "phase0"}])
            await phase0.workflow_service.respond_activity_task_completed(
                RespondActivityTaskCompletedRequest(namespace=NAMESPACE, task_token=task.task_token, result=Payloads(payloads=payload))
            )
            assert await handle.result() == {"by": "phase0"}

    run(go())


def test_worker_runs_the_noop_activity():
    """The happy path through the SDK: what an HTCondor job does in question 1."""

    async def go():
        controller = await client_for("controller")
        unit = await client_for("ewkit-phase0")
        async with (
            Worker(controller, task_queue=CONTROLLER_QUEUE, workflows=[NoopWorkflow]),
            Worker(unit, task_queue=UNIT_QUEUE, activities=[activity.defn(name="probe_env")(probe_env)]),
        ):
            report = await controller.execute_workflow(
                NoopWorkflow.run, ProbeRequest(task_queue=UNIT_QUEUE, work_seconds=1, wait_hours=0.1),
                id=f"dataset/phase0/authz-noop-{uuid.uuid4()}", task_queue=CONTROLLER_QUEUE,
            )
        assert report["heartbeats"] >= 1

    run(go())


def test_reader_reads_but_cannot_change_workflows():
    async def go():
        controller = await client_for("controller")
        reader = await client_for("reader")
        workflow_id = await start_sleeping_workflow(controller)
        handle = reader.get_workflow_handle(workflow_id)
        try:
            assert (await handle.describe()).id == workflow_id
            await reader.count_workflows()
            await reader.workflow_service.get_workflow_execution_history(
                GetWorkflowExecutionHistoryRequest(namespace=NAMESPACE, execution=WorkflowExecution(workflow_id=workflow_id))
            )
            await assert_denied(reader.start_workflow("Sleeping", id=f"{workflow_id}-2", task_queue="x"))
            await assert_denied(handle.signal("anything"))
            await assert_denied(handle.cancel())
            await assert_denied(handle.terminate("reader says so"))
            await assert_denied(reader.workflow_service.poll_activity_task_queue(poll_activity(UNIT_QUEUE), timeout=SHORT))
        finally:
            await controller.get_workflow_handle(workflow_id).terminate("authz test")

    run(go())


@pytest.mark.parametrize(
    "name",
    [
        "root-signed-worker",  # a worker subject, but not from the worker CA
        "worker-ca-controller",  # what a leaked worker CA key could mint
    ],
)
def test_roles_are_checked_against_the_issuing_ca(name):
    """The handshake accepts these (they chain to the root); the claim mapper refuses every call."""
    with pytest.raises(RuntimeError, match="(?i)PermissionDenied|unauthorized"):
        run(client_for(name))


@pytest.mark.parametrize("name, with_client_cert", [("controller", False), ("foreign", True)])
def test_handshake_rejects_missing_or_foreign_certificates(name, with_client_cert):
    with pytest.raises(Exception):
        run(client_for(name, with_client_cert=with_client_cert))


def test_admin_alone_registers_namespaces():
    async def go():
        controller = await client_for("controller")
        await assert_denied(controller.workflow_service.register_namespace(RegisterNamespaceRequest(namespace=f"x{uuid.uuid4().hex[:8]}")))

    run(go())

"""Start a NoopWorkflow as the controller and wait for a worker to run its activity.

    python -m shim.start_noop [--queue ewkit-phase0-default] [--work-seconds 30]

Runs the workflow worker for the controller queue in this process while it waits, so
nothing else needs to be running on the controller side. Prints the activity's report and
how long the task waited for a worker (schedule-to-start).
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone

from temporalio.worker import Worker

from shim.common import CONTROLLER_QUEUE, UNIT_QUEUE, add_connection_args, certs_root, connect, server_name
from shim.workflows import NoopWorkflow, ProbeRequest


async def run(args: argparse.Namespace) -> dict:
    client = await connect(args.address, args.certs, server_name(args))
    # Workflow IDs follow the production plan's scheme; worker certificates are scoped to them
    workflow_id = args.id or f"dataset/phase0/noop-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    req = ProbeRequest(task_queue=args.queue, work_seconds=args.work_seconds, wait_hours=args.wait_hours)
    async with Worker(client, task_queue=CONTROLLER_QUEUE, workflows=[NoopWorkflow]):
        handle = await client.start_workflow(NoopWorkflow.run, req, id=workflow_id, task_queue=CONTROLLER_QUEUE)
        print(f"started {workflow_id}; waiting for a worker on {args.queue}", flush=True)
        report = await handle.result()
    waited = datetime.fromisoformat(report["started_time"]) - datetime.fromisoformat(report["scheduled_time"])
    return {"workflow_id": workflow_id, "schedule_to_start_seconds": round(waited.total_seconds(), 1), "report": report}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_args(parser, default_cert_dir=str(certs_root() / "clients" / "controller"))
    parser.add_argument("--queue", default=UNIT_QUEUE)
    parser.add_argument("--work-seconds", type=int, default=30)
    parser.add_argument("--wait-hours", type=float, default=6.0)
    parser.add_argument("--id", help="workflow ID (default dataset/phase0/noop-<time>)")
    print(json.dumps(asyncio.run(run(parser.parse_args())), indent=2))


if __name__ == "__main__":
    main()

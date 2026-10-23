"""Register the phase 0 namespace with the admin certificate.

    uv run python -m shim.admin [--address host:7233] [--retention-days 3]
"""

import argparse
import asyncio
from datetime import timedelta

from google.protobuf.duration_pb2 import Duration
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, RegisterNamespaceRequest
from temporalio.service import RPCError, RPCStatusCode

from shim.common import NAMESPACE, add_connection_args, certs_root, connect, server_name


async def register(args: argparse.Namespace) -> None:
    client = await connect(args.address, args.certs, server_name(args), namespace=args.namespace)
    retention = Duration()
    retention.FromTimedelta(timedelta(days=args.retention_days))
    try:
        await client.workflow_service.register_namespace(
            RegisterNamespaceRequest(
                namespace=args.namespace,
                description="agentic-ops phase 0 tests",
                workflow_execution_retention_period=retention,
            )
        )
        print(f"registered namespace {args.namespace}")
    except RPCError as e:
        if e.status != RPCStatusCode.ALREADY_EXISTS:
            raise
        print(f"namespace {args.namespace} already exists")
    # Registration reaches the frontend's namespace cache after a few seconds
    for _ in range(30):
        try:
            await client.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=args.namespace))
            return
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                raise
            await asyncio.sleep(1)
    raise SystemExit(f"namespace {args.namespace} still not visible")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_args(parser, default_cert_dir=str(certs_root() / "clients" / "admin"))
    parser.add_argument("--namespace", default=NAMESPACE)
    parser.add_argument("--retention-days", type=int, default=3)
    asyncio.run(register(parser.parse_args()))


if __name__ == "__main__":
    main()

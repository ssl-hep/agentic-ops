r"""Register a namespace, and optionally the production search attributes, with the admin certificate.

    uv run python -m shim.admin [--address host:7233] [--retention-days 3]
    uv run python -m shim.admin --address <LB IP>:7233 --server-name <name> \
        --certs secrets/temporal-pki/clients/admin \
        --namespace ewkit-production --retention-days 30 --search-attributes   # phase 1
"""

import argparse
import asyncio
from datetime import timedelta

from google.protobuf.duration_pb2 import Duration
from temporalio.api.enums.v1 import IndexedValueType
from temporalio.api.operatorservice.v1 import AddSearchAttributesRequest, ListSearchAttributesRequest
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, RegisterNamespaceRequest
from temporalio.service import RPCError, RPCStatusCode

from shim.common import NAMESPACE, add_connection_args, certs_root, connect, server_name

# The production workflows' search attributes (docs/temporal-production-plan.md, Workflows)
SEARCH_ATTRIBUTES = {
    "ProductionId": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "Dataset": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "DatasetState": IndexedValueType.INDEXED_VALUE_TYPE_KEYWORD,
    "UnitsDone": IndexedValueType.INDEXED_VALUE_TYPE_INT,
    "UnitsTotal": IndexedValueType.INDEXED_VALUE_TYPE_INT,
    "FilesWithoutReplica": IndexedValueType.INDEXED_VALUE_TYPE_INT,
    "NeedsDecision": IndexedValueType.INDEXED_VALUE_TYPE_BOOL,
}


async def register(args: argparse.Namespace) -> None:
    client = await connect(args.address, args.certs, server_name(args), namespace=args.namespace)
    retention = Duration()
    retention.FromTimedelta(timedelta(days=args.retention_days))
    try:
        await client.workflow_service.register_namespace(
            RegisterNamespaceRequest(
                namespace=args.namespace,
                description=args.description,
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
            break
        except RPCError as e:
            if e.status != RPCStatusCode.NOT_FOUND:
                raise
            await asyncio.sleep(1)
    else:
        raise SystemExit(f"namespace {args.namespace} still not visible")
    if args.search_attributes:
        existing = await client.operator_service.list_search_attributes(
            ListSearchAttributesRequest(namespace=args.namespace)
        )
        missing = {k: v for k, v in SEARCH_ATTRIBUTES.items() if k not in existing.custom_attributes}
        if missing:
            await client.operator_service.add_search_attributes(
                AddSearchAttributesRequest(namespace=args.namespace, search_attributes=missing)
            )
        print(f"search attributes added: {sorted(missing) or 'none'}; already there: "
              f"{sorted(SEARCH_ATTRIBUTES.keys() - missing.keys()) or 'none'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_args(parser, default_cert_dir=str(certs_root() / "clients" / "admin"))
    parser.add_argument("--namespace", default=NAMESPACE)
    parser.add_argument("--retention-days", type=int, default=3)
    parser.add_argument("--description", default="agentic-ops phase 0 tests")
    parser.add_argument(
        "--search-attributes", action="store_true", help="also register the production search attributes"
    )
    asyncio.run(register(parser.parse_args()))


if __name__ == "__main__":
    main()

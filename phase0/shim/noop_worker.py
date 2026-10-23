"""Phase 0 activity worker: serves probe_env, which heartbeats and reports where it ran.

    python -m shim.noop_worker --certs DIR [--queue ewkit-phase0-default] [--max-tasks 1]

Runs on a laptop, in a container or in an HTCondor job (jobs/bootstrap.sh). It exits after
--max-tasks activities or --idle-timeout seconds without one, whichever comes first, and
prints a JSON summary. It registers no workflows, so it never polls a workflow task queue.
"""

import argparse
import asyncio
import json
import os
import platform
import pwd
import shutil
import socket
import sys
import time
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from temporalio import activity
from temporalio.worker import Worker

from shim.common import UNIT_QUEUE, add_connection_args, connect, server_name
from shim.workflows import ProbeRequest

# Job and machine ClassAd attributes worth recording; anything about walltime, runtime or
# retirement is added on top, since that is what question 7 is after.
JOB_AD_KEYS = {
    "ClusterId", "ProcId", "Owner", "User", "AcctGroup", "AccountingGroup", "RemoteHost",
    "QDate", "JobStartDate", "JobCurrentStartDate", "RequestCpus", "RequestMemory",
    "RequestDisk", "x509userproxysubject", "x509UserProxyVOName", "Queue",
}
MACHINE_AD_KEYS = {
    "Machine", "OpSysAndVer", "OpSysName", "Cpus", "Memory", "TotalCpus", "TotalMemory",
    "TotalSlotCpus", "TotalSlotMemory", "HasSingularity", "CVMFS_atlas_cern_ch_REVISION",
}
TIME_LIMIT_HINTS = ("wall", "runtime", "retire", "lifetime", "maxjob")


def read_class_ad(path: str | None, keys: set[str]) -> dict[str, str]:
    if not path or not Path(path).is_file():
        return {}
    ad = {}
    for line in Path(path).read_text(errors="replace").splitlines():
        name, sep, value = line.partition(" = ")
        if sep and (name in keys or any(h in name.lower() for h in TIME_LIMIT_HINTS)):
            ad[name] = value.strip().strip('"')
    return ad


def path_facts(path: str) -> dict:
    p = Path(path)
    facts: dict = {"exists": p.exists(), "is_dir": p.is_dir()}
    if p.is_dir():
        try:
            usage = shutil.disk_usage(p)
            facts["free_gb"] = round(usage.free / 1e9, 1)
        except OSError as e:
            facts["error"] = str(e)
    return facts


def version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def os_name() -> str:
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", platform.platform())
    except OSError:
        return platform.platform()


def user_name(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


async def probe_env(req: ProbeRequest) -> dict:
    """The probe_env activity body. serve() registers it under that name."""
    info = activity.info()
    deadline = time.monotonic() + req.work_seconds
    beats = 0
    while time.monotonic() < deadline:
        beats += 1
        activity.heartbeat(beats)
        await asyncio.sleep(min(5, max(0, deadline - time.monotonic())))

    uid = os.getuid()
    return {
        "host": socket.getfqdn(),
        "os": os_name(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "temporalio": version("temporalio"),
        "uproot": version("uproot"),
        "uid": uid,
        "gid": os.getgid(),
        "user": user_name(uid),
        "cwd": os.getcwd(),
        "scratch": os.environ.get("_CONDOR_SCRATCH_DIR"),
        "x509_user_proxy_set": "X509_USER_PROXY" in os.environ,
        "paths": {p: path_facts(p) for p in ("/data", "/cvmfs/atlas.cern.ch")},
        "job_ad": read_class_ad(os.environ.get("_CONDOR_JOB_AD"), JOB_AD_KEYS),
        "machine_ad": read_class_ad(os.environ.get("_CONDOR_MACHINE_AD"), MACHINE_AD_KEYS),
        "attempt": info.attempt,
        "heartbeats": beats,
        "workflow_id": info.workflow_id,
        "scheduled_time": info.current_attempt_scheduled_time.isoformat(),
        "started_time": info.started_time.isoformat(),
        "finished_time": datetime.now(timezone.utc).isoformat(),
    }


async def serve(args: argparse.Namespace) -> dict:
    client = await connect(args.address, args.certs, server_name(args))
    done = asyncio.Event()
    completed = 0
    @activity.defn(name="probe_env")
    async def counted_probe(req: ProbeRequest) -> dict:
        nonlocal completed
        try:
            return await probe_env(req)
        finally:
            completed += 1
            if completed >= args.max_tasks:
                done.set()

    worker = Worker(
        client,
        task_queue=args.queue,
        activities=[counted_probe],
        max_concurrent_activities=args.activity_slots,
    )
    started = time.monotonic()
    run = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(done.wait(), timeout=args.idle_timeout)
        reason = "max-tasks"
    except TimeoutError:
        reason = "idle-timeout"
    await worker.shutdown()
    await run
    return {
        "queue": args.queue,
        "address": args.address,
        "completed": completed,
        "stopped_by": reason,
        "seconds": round(time.monotonic() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_connection_args(parser)
    parser.add_argument("--queue", default=UNIT_QUEUE)
    parser.add_argument("--max-tasks", type=int, default=1)
    parser.add_argument("--idle-timeout", type=float, default=1800, help="seconds to wait for work")
    parser.add_argument("--activity-slots", type=int, default=1)
    summary = asyncio.run(serve(parser.parse_args()))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

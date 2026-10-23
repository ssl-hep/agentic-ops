"""Question 2: can a pod submit to the AF schedd with an IDTOKEN, and as which identity?

Environment:
  AF_COLLECTOR   the AF pool's collector, host[:port] (from probes/af_limits.sh)
  AF_SCHEDD      the schedd to submit to (default: the first one the collector lists)
  WAIT_MINUTES   how long to wait for the job to finish (default 30)
  _condor_SEC_TOKEN_DIRECTORY   directory holding the IDTOKEN
  CONDOR_DEBUG   set (with _condor_TOOL_DEBUG=D_SECURITY) to log the security handshake

Pings the schedd for WRITE (which reports the identity it maps us to), submits one spooled
job that prints `id`, `hostname -f` and whether /data is visible, waits, retrieves its
output and prints the job attributes that say who ran it. Runs the same on a laptop.
"""

import os
import sys
import time

import htcondor2 as htcondor

ATTRS = [
    "ClusterId", "JobStatus", "Owner", "User", "AuthTokenSubject", "AuthTokenIssuer",
    "AuthTokenId", "AcctGroup", "AccountingGroup", "RemoteHost", "LastRemoteHost",
    "ExitCode", "HoldReason", "HoldReasonCode", "QDate", "JobCurrentStartDate", "CompletionDate",
]

SPOOLING_INPUT = 16


def main():
    if os.environ.get("CONDOR_DEBUG"):
        htcondor.enable_debug()
    pool = os.environ["AF_COLLECTOR"]
    collector = htcondor.Collector(pool)
    schedd_name = os.environ.get("AF_SCHEDD")
    schedd_ad = collector.locate(htcondor.DaemonType.Schedd, schedd_name) if schedd_name else collector.locate(htcondor.DaemonType.Schedd)
    print("schedd:", schedd_ad.get("Name"), schedd_ad.get("MyAddress"))

    ping = htcondor.ping(schedd_ad, "WRITE")
    print("ping WRITE:", {k: ping.get(k) for k in ("AuthorizationSucceeded", "MyRemoteUserName", "AuthMethods", "Encryption", "RemoteVersion")})
    if not ping.get("AuthorizationSucceeded"):
        sys.exit("not authorized to submit")

    schedd = htcondor.Schedd(schedd_ad)
    submit = htcondor.Submit({
        "executable": "/bin/sh",
        "transfer_executable": "false",
        "arguments": "\"-c 'id; hostname -f; ls -ld /data /data/agentic-ops 2>&1; echo X509_USER_PROXY=$X509_USER_PROXY'\"",
        "output": "probe.out",
        "error": "probe.err",
        "should_transfer_files": "YES",
        "when_to_transfer_output": "ON_EXIT",
        "request_cpus": "1",
        "request_memory": "256MB",
        "+AgenticOpsProbe": '"phase0"',
        # Keep the finished job until its output has been retrieved, as condor_submit -spool does
        "leave_in_queue": "(JobStatus == 4) && ((StageOutFinish =?= UNDEFINED) || (StageOutFinish == 0))",
    })
    result = schedd.submit(submit, count=1, spool=True)
    schedd.spool(result)
    cluster = result.cluster()
    print("submitted cluster", cluster, flush=True)

    deadline = time.time() + 60 * float(os.environ.get("WAIT_MINUTES", "30"))
    ad = {}
    while time.time() < deadline:
        ads = schedd.query(f"ClusterId == {cluster}", ATTRS)
        if not ads:
            ads = schedd.history(f"ClusterId == {cluster}", ATTRS, match=1)
        ad = ads[0] if ads else {}
        status = ad.get("JobStatus")
        print(time.strftime("%H:%M:%S"), "JobStatus", status, flush=True)
        # Removed, completed, or held for any reason but the input spooling that follows submit
        if status in (3, 4) or status == 5 and ad.get("HoldReasonCode") != SPOOLING_INPUT:
            break
        time.sleep(15)

    print("job:", {k: ad[k] for k in ATTRS if k in ad and not isinstance(ad[k], htcondor.classad.Value)})
    if ad.get("JobStatus") == 4:
        schedd.retrieve(f"ClusterId == {cluster}")
        for name in ("probe.out", "probe.err"):
            if os.path.exists(name):
                print(f"--- {name}\n{open(name).read()}")
    else:
        schedd.act(htcondor.JobAction.Remove, f"ClusterId == {cluster}")
        sys.exit("job did not complete")


if __name__ == "__main__":
    main()

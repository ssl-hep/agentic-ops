"""Questions 3 and 4: what the account can see in Rucio, and where a DID's files can be read.

Read-only: nothing here creates rules or changes anything in Rucio. On the AF login node,
after `setupATLAS; lsetup rucio; voms-proxy-init -voms atlas`:

    python3 rucio_replicas.py whoami
    python3 rucio_replicas.py pfns SCOPE:NAME [--per-rse 2] [--out pfns.txt]

`pfns` lists the DID's files and their replicas the way snapshot_dataset will: disk replicas
as `root://` PFNs, ordered MWT2 first, then other US sites, then the rest; tape replicas are
counted but never used. It writes up to --per-rse files from each RSE that holds any, so
jobs/replica_read.sub can compare MWT2 with remote sites. Each line is
`<rse> <preference> <bytes> <pfn>`. Written for Python 3.9 (EL9's system python).
"""

import argparse
import collections
import json
import sys

from rucio.client import Client

LOCAL_SITE = "MWT2"


def split_did(did):
    scope, sep, name = did.partition(":")
    if not sep:
        sys.exit("give the DID as scope:name")
    return scope, name


def whoami(client, args):
    me = client.whoami()
    account = me["account"]
    attributes = [(a["key"], a["value"]) for batch in client.list_account_attributes(account) for a in batch]
    print(json.dumps({"account": account, "status": me.get("status"), "email": me.get("email"), "attributes": attributes}, indent=2))


def rse_preference(client, rse, cache={}):
    """0 for the AF's own site, 1 for other US sites, 2 for everything else."""
    if rse not in cache:
        attrs = client.list_rse_attributes(rse)
        if attrs.get("site") == LOCAL_SITE or rse.startswith(LOCAL_SITE + "_"):
            cache[rse] = 0
        elif attrs.get("cloud") == "US" or attrs.get("country") == "us":
            cache[rse] = 1
        else:
            cache[rse] = 2
    return cache[rse]


def pfns(client, args):
    scope, name = split_did(args.did)
    files = disk_files = 0
    tape_only = []
    per_rse = collections.Counter()
    picked = collections.defaultdict(list)
    for rep in client.list_replicas([{"scope": scope, "name": name}], schemes=["root"], all_states=False):
        files += 1
        disk = [(meta["rse"], pfn) for pfn, meta in rep.get("pfns", {}).items() if meta.get("type") == "DISK"]
        if not disk:
            tape_only.append(rep["name"])
            continue
        disk_files += 1
        for rse, pfn in disk:
            per_rse[rse] += 1
            if len(picked[rse]) < args.per_rse:
                picked[rse].append((rse_preference(client, rse), rep.get("bytes") or 0, pfn))

    lines = sorted((pref, rse, size, pfn) for rse, rows in picked.items() for pref, size, pfn in rows)
    with open(args.out, "w") as f:
        f.write("# {} files of {}; up to {} per RSE; <rse> <preference> <bytes> <pfn>\n".format(files, args.did, args.per_rse))
        for pref, rse, size, pfn in lines:
            f.write("{} {} {} {}\n".format(rse, pref, size, pfn))

    print(json.dumps({
        "did": args.did,
        "files": files,
        "files_with_disk_replica": disk_files,
        "tape_only_files": len(tape_only),
        "disk_replicas_per_rse": {r: n for r, n in per_rse.most_common()},
        "rse_preference": {r: rse_preference(client, r) for r in per_rse},
        "pfns_written": len(lines),
        "out": args.out,
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("whoami")
    p = sub.add_parser("pfns")
    p.add_argument("did")
    p.add_argument("--per-rse", type=int, default=2, help="files to write per RSE")
    p.add_argument("--out", default="pfns.txt")
    args = parser.parse_args()
    {"whoami": whoami, "pfns": pfns}[args.command](Client(), args)


if __name__ == "__main__":
    main()

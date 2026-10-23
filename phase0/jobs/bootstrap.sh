#!/bin/bash
# HTCondor job entry point for phase 0 workers.
#
#   bootstrap.sh TARBALL SHA256 MODULE [ARGS...]
#
# Copies TARBALL (built by workerenv/build.sh, kept on /data) into the job's scratch
# directory, checks its digest, unpacks it and runs `python -m MODULE ARGS...` with the
# bundled Python. The tarball is read from /data by the job itself because the submitter
# (eventually a k8s pod) cannot see /data. Certificates arrive by file transfer.
set -euo pipefail

tarball=$1 digest=$2 module=$3
shift 3
scratch=${_CONDOR_SCRATCH_DIR:-$(mktemp -d)}
start=$(date +%s%3N)
stamp() { local ms=$(($(date +%s%3N) - start)); printf '[bootstrap %d.%03ds] %s\n' $((ms / 1000)) $((ms % 1000)) "$*"; }

stamp "host $(hostname -f) uid $(id -u) scratch $scratch"
cp "$tarball" "$scratch/worker-env.tar.gz"
stamp "copied $(du -h "$scratch/worker-env.tar.gz" | cut -f1) from $tarball"
if ! echo "$digest  $scratch/worker-env.tar.gz" | sha256sum --check --quiet; then
  echo "worker environment digest does not match $digest" >&2
  exit 1
fi
tar -C "$scratch" -xzf "$scratch/worker-env.tar.gz"
rm "$scratch/worker-env.tar.gz"
stamp "unpacked; starting $module"

export PYTHONPATH="$scratch/worker-env/app" PYTHONNOUSERSITE=1
exec "$scratch/worker-env/python/bin/python3" -m "$module" "$@"

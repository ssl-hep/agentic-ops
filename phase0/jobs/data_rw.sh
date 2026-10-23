#!/bin/bash
# Question 5: is /data mounted in jobs, and as whom does a job write there?
#   data_rw.sh ROOT [MEGABYTES]
# Writes MEGABYTES (default 100) under ROOT/<cluster>.<proc>, reads it back, compares
# checksums, shows the numeric owner, deletes it. Also reports whether the plan's shared
# root /data/agentic-ops exists and is writable.
set -uo pipefail
root=$1 mb=${2:-100}
dir="$root/${CLUSTER:-0}.${PROC:-0}"

echo "== identity";  id; hostname -f
# User directories under /data can be symlinks into /cold; report where ROOT really lives
mkdir -p "$root"
echo "== $root resolves to $(readlink -f "$root")"
echo "== mounts";    findmnt -T /data -o TARGET,SOURCE,FSTYPE 2>&1; findmnt -T "$root" -o TARGET,SOURCE,FSTYPE 2>&1
echo "== /data";     ls -ld /data /data/agentic-ops "$root" 2>&1
echo "== /data/agentic-ops writable: $([[ -w /data/agentic-ops ]] && echo yes || echo no)"

echo "== write $mb MB to $dir"
mkdir -p "$dir" || exit 1
start=$(date +%s%3N)
dd if=/dev/urandom of="$dir/probe.bin" bs=1M count="$mb" status=none || exit 1
sync
written=$(sha256sum "$dir/probe.bin" | cut -d" " -f1)
echo "wrote in $(( $(date +%s%3N) - start )) ms, sha256 $written"
ls -ln "$dir/probe.bin"
stat -c 'owner %U(%u) group %G(%g) mode %a' "$dir/probe.bin"

echo "== read back"
start=$(date +%s%3N)
read_back=$(cat "$dir/probe.bin" | sha256sum | cut -d' ' -f1)
echo "read in $(( $(date +%s%3N) - start )) ms, sha256 $read_back"
[[ $read_back == "$written" ]] && echo "checksums match" || { echo "CHECKSUM MISMATCH"; status=1; }

echo "== delete"
rm -f "$dir/probe.bin" && rmdir "$dir" && echo "deleted $dir"
exit ${status:-0}

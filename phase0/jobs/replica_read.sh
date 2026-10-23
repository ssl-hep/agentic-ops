#!/bin/bash
# Question 4: can AF jobs read DAODs in place over root://, from MWT2 and from other sites,
# directly and through the AF XCache? With or without a proxy? How fast?
#   replica_read.sh PFNS_FILE [XCACHE]
# PFNS_FILE has "<rse> <preference> <bytes> <pfn>" lines from probes/rucio_replicas.py pfns.
# XCACHE defaults to root://xcache.af.uchicago.edu:1094. Every PFN is copied to /dev/null
# four ways (direct or via XCache, with or without the job's proxy). One line per try:
#   RESULT <direct|xcache> <proxy|noproxy> <ok|FAIL> <seconds> <MB/s> <rse> <pfn>
set -uo pipefail
pfns=$1 xcache=${2:-root://xcache.af.uchicago.edu:1094}

echo "== identity"; id; hostname -f
echo "== proxy: X509_USER_PROXY=${X509_USER_PROXY:-unset}"
if ! command -v xrdcp >/dev/null; then
  export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase
  # ALRB reads variables it never sets, which set -u turns into a fatal error
  set +u
  # shellcheck disable=SC1091
  source "$ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh" --quiet >/dev/null
  lsetup xrootd >/dev/null
  set -u
fi
command -v xrdcp >/dev/null || { echo "no xrdcp, even after lsetup xrootd" >&2; exit 1; }
echo "== xrdcp: $(command -v xrdcp) $(xrdcp --version 2>&1)"
echo "== xcache: $xcache"

proxy=${X509_USER_PROXY:-}
try() { # try <direct|xcache> <proxy|noproxy> <rse> <bytes> <url>
  local how=$1 mode=$2 rse=$3 bytes=$4 url=$5 start out status ms
  start=$(date +%s%3N)
  if [[ $mode == noproxy ]]; then
    out=$(env -u BEARER_TOKEN -u BEARER_TOKEN_FILE X509_USER_PROXY=/nonexistent xrdcp -f -s "$url" /dev/null 2>&1); status=$?
  else
    out=$(xrdcp -f -s "$url" /dev/null 2>&1); status=$?
  fi
  ms=$(( $(date +%s%3N) - start ))
  [[ -n $out ]] && echo "$out" | tail -2 | sed 's/^/    /'
  printf 'RESULT %s %s %s %d.%03d %s %s %s\n' "$how" "$mode" "$([[ $status == 0 ]] && echo ok || echo FAIL)" \
    $((ms / 1000)) $((ms % 1000)) "$( ((status == 0 && ms > 0)) && echo $((bytes / 1000 / ms)) || echo -)" "$rse" "$url"
}

while read -r rse _pref bytes pfn; do
  [[ -z $rse || $rse == \#* ]] && continue
  for mode in proxy noproxy; do
    [[ $mode == proxy && -z $proxy ]] && continue
    try direct "$mode" "$rse" "$bytes" "$pfn"
    # XCache is a forward proxy: the origin URL goes after its own, behind a double slash
    try xcache "$mode" "$rse" "$bytes" "$xcache//$pfn"
  done
done < "$pfns"

#!/usr/bin/env bash
# Question 7 (walltime and memory limits) plus the pool facts the other probes need: the
# collector and schedd names (question 2) and how slots are shaped.
#   probes/af_limits.sh > af_limits.$(date +%F).txt      # on the AF login node
set -uo pipefail
section() { printf '\n== %s\n' "$*"; }

section "HTCondor version and pool"
condor_version | head -1
condor_config_val COLLECTOR_HOST CONDOR_HOST SCHEDD_NAME UID_DOMAIN FILESYSTEM_DOMAIN 2>&1
section "collectors";  condor_status -collector -af Name 2>&1
section "schedds";     condor_status -schedd -af Name ScheddIpAddr TotalRunningJobs TotalIdleJobs 2>&1

section "slot shapes (count, cpus, memory MB, os)"
condor_status -af Cpus Memory OpSysAndVer 2>/dev/null | sort | uniq -c | sort -rn | head -20
section "partitionable machines (count, total cpus, total memory MB)"
condor_status -constraint PartitionableSlot -af TotalCpus TotalMemory 2>/dev/null | sort | uniq -c | sort -rn | head -20
section "largest memory a single slot offers (MB)"
condor_status -af TotalSlotMemory 2>/dev/null | sort -n | tail -1

# Ask the running schedd for its configuration, falling back to this node's. An empty or
# missing knob reads as "not set": locally condor_config_val fails, and through -schedd it
# prints "Not defined: ..." on stdout and succeeds.
schedd_config() {
  local out
  out=$(condor_config_val -schedd "$@" 2>/dev/null || condor_config_val "$@" 2>/dev/null)
  [[ $out == "Not defined"* ]] || printf '%s\n' "$out"
}

section "schedd and startd policy that limits runtime or memory"
schedd_config -dump | grep -iE '^[A-Z_.]*(MAXJOBRETIREMENTTIME|MAXWALLTIME|MAXRUNTIME|MAX_?JOB_?RUNTIME|SYSTEM_PERIODIC_(REMOVE|HOLD)|PERIODIC_REMOVE|SUBMIT_REQUIREMENT|JOB_TRANSFORM|DEFAULT_.*(WALL|RUNTIME|MEMORY)|QUEUE)[A-Z_]* *=' | sort
section "job transforms and submit requirements in effect on the schedd"
for knob in JOB_TRANSFORM_NAMES SUBMIT_REQUIREMENT_NAMES; do
  names=$(schedd_config "$knob")
  if [[ -z $names ]]; then
    echo "$knob: not set"
    continue
  fi
  echo "$knob = $names"
  prefix=${knob%_NAMES}
  for name in ${names//,/ }; do
    echo "  ${prefix}_$name = $(schedd_config "${prefix}_$name" | head -c 600)"
  done
done

section "your recent jobs: requested vs used, runtime (hours)"
condor_history "$USER" -limit 200 -af RequestMemory MemoryUsage RemoteWallClockTime JobStatus 2>/dev/null \
  | awk '{printf "%s %s %.1f %s\n", $1, $2, $3/3600, $4}' | sort | uniq -c | sort -rn | head -15

section "IDTOKENs this account holds (question 2)"
condor_token_list 2>&1 | head -20

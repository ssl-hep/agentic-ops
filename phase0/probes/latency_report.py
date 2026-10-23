"""Question 6: start latency of the jobs from jobs/latency.sub.

    python3 latency_report.py jobs/logs/latency.*.log

Reads each run's HTCondor event log: "000" (submitted) and the first "001" (executing) event
of every job. The schedd's history on the AF turns over within a day, so the log is the record
that lasts. Prints one line per run: when it was submitted, how many jobs started, and the
p50, p90 and max wait. Python 3.9, standard library only.
"""

import re
import statistics
import sys
from datetime import datetime

EVENT = re.compile(r"^(000|001) \((\d+)\.(\d+)\.\d+\) (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")


def waits(path):
    submitted, started = {}, {}
    with open(path) as f:
        for line in f:
            m = EVENT.match(line)
            if not m:
                continue
            code, job, when = m.group(1), (m.group(2), m.group(3)), datetime.strptime(m.group(4), "%Y-%m-%d %H:%M:%S")
            (submitted if code == "000" else started).setdefault(job, when)
    return submitted, {job: (started[job] - t).total_seconds() for job, t in submitted.items() if job in started}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for path in sys.argv[1:]:
        submitted, wait = waits(path)
        if not submitted:
            print("{}: no submit events".format(path))
            continue
        line = "{} submitted {:%Y-%m-%d %H:%M}: {} jobs, {} started".format(path, min(submitted.values()), len(submitted), len(wait))
        if wait:
            w = sorted(wait.values())
            p90 = statistics.quantiles(w, n=10)[8] if len(w) > 1 else w[0]
            line += "; wait p50 {:.0f} s, p90 {:.0f} s, max {:.0f} s".format(statistics.median(w), p90, w[-1])
        print(line)


if __name__ == "__main__":
    main()

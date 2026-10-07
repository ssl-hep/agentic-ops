import type { AnalysisStatus } from "@/lib/api";

const LABELS: Record<AnalysisStatus, string> = {
  runnable: "Runnable",
  blocked: "Blocked",
  config_only: "Config only",
  invalid: "Invalid",
};

export function StatusBadge({ status }: { status: AnalysisStatus }) {
  return <span className={`badge badge-${status}`}>{LABELS[status]}</span>;
}

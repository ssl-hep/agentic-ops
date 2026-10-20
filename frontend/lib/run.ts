// Run configuration: the parameters a physicist picks on the "Configure run" screen, and the
// parsing shared by the form's Server Function. Nothing here submits anything yet.

export type Queue = "short" | "long";

export interface RunRequest {
  folder: string;
  /** Commit SHA the run is pinned to, so its inputs cannot change underneath it */
  sha: string;
  ref: string;
  config: string;
  sample_lists: string[];
  campaigns: string[];
  /** Dataset agent selection that replaces sample_lists and campaigns when set */
  selection_id?: string;
  queue: Queue;
  allow_mwt2_overflow: boolean;
  files_per_job: number;
  memory_gb: number;
  /** Events per job for the full run; null reads every event */
  max_events: number | null;
  test_job: boolean;
  test_events: number;
  max_jobs: number;
  max_core_hours: number;
  output_name: string;
  notes: string;
}

export const QUEUES: Record<Queue, string> = {
  short: "Short (4 h limit)",
  long: "Long (72 h limit)",
};

export const RUN_DEFAULTS = {
  queue: "short" as Queue,
  allow_mwt2_overflow: false,
  files_per_job: 5,
  memory_gb: 2,
  test_job: true,
  test_events: 1000,
  max_jobs: 2000,
  max_core_hours: 5000,
};

export type RunFormState =
  | { status: "idle" }
  | { status: "invalid"; errors: string[] }
  | { status: "noop"; request: RunRequest };

function int(form: FormData, key: string, label: string, errors: string[], min = 1): number {
  const raw = String(form.get(key) ?? "").trim();
  const value = Number(raw);
  if (!raw || !Number.isInteger(value) || value < min) {
    errors.push(`${label} must be a whole number of at least ${min}.`);
    return min;
  }
  return value;
}

export function parseRunForm(form: FormData): { request: RunRequest; errors: string[] } {
  const errors: string[] = [];
  const text = (key: string) => String(form.get(key) ?? "").trim();
  const queue = text("queue");
  const testJob = form.get("test_job") === "on";
  const maxEvents = text("max_events");
  const selectionId = text("selection_id");

  const request: RunRequest = {
    folder: text("folder"),
    sha: text("sha"),
    ref: text("ref"),
    config: text("config"),
    sample_lists: form.getAll("sample_lists").map(String),
    campaigns: form.getAll("campaigns").map(String),
    selection_id: selectionId || undefined,
    queue: queue === "long" ? "long" : "short",
    allow_mwt2_overflow: form.get("allow_mwt2_overflow") === "on",
    files_per_job: int(form, "files_per_job", "Files per job", errors),
    memory_gb: int(form, "memory_gb", "Memory per job", errors),
    max_events: maxEvents ? int(form, "max_events", "Events per job", errors) : null,
    test_job: testJob,
    test_events: testJob ? int(form, "test_events", "Test job events", errors) : RUN_DEFAULTS.test_events,
    max_jobs: int(form, "max_jobs", "Job cap", errors),
    max_core_hours: int(form, "max_core_hours", "Core-hour cap", errors),
    output_name: text("output_name"),
    notes: text("notes"),
  };

  if (!request.config) errors.push("Pick a config.");
  if (!selectionId) {
    if (request.sample_lists.length === 0) errors.push("Pick at least one sample list.");
    if (request.campaigns.length === 0) errors.push("Pick at least one campaign.");
  }
  if (!/^[A-Za-z0-9._-]+$/.test(request.output_name)) {
    errors.push("Output name may only use letters, digits, dots, dashes and underscores.");
  }
  return { request, errors };
}

// Server-side client for the FastAPI backend. Only imported by server components, so the
// backend URL never reaches the browser and the browser never talks to the backend directly.
import "server-only";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8000";

export type AnalysisStatus = "runnable" | "blocked" | "config_only" | "invalid";

export interface ConnectionStatus {
  gitlab_url: string;
  project: string;
  token_configured: boolean;
  reachable: boolean;
  project_web_url: string | null;
  default_branch: string | null;
  detail: string | null;
}

export interface RefOption {
  name: string;
  kind: "branch" | "tag";
  commit_sha: string;
  committed_date: string | null;
  protected: boolean;
  default: boolean;
}

export interface RefList {
  default_branch: string;
  branches: RefOption[];
  tags: RefOption[];
}

export interface ResolvedRef {
  ref: string;
  sha: string;
  short_sha: string;
  title: string;
  author_name: string | null;
  committed_date: string | null;
  web_url: string;
}

export interface ImageStatus {
  tag: string | null;
  location: string | null;
  available: boolean | null;
  digest: string | null;
  created_at: string | null;
  detail: string | null;
}

export interface SampleListSummary {
  name: string;
  run: string;
  sample: string;
  path: string;
  web_url: string;
  datasets: number;
  groups: number;
  campaigns: Record<string, number>;
  problems: number;
}

export interface ExtendStep {
  path: string;
  exists: boolean;
  web_url: string | null;
}

export interface ConfigInfo {
  name: string;
  path: string;
  web_url: string;
  extend_chain: ExtendStep[];
  blocks: string[];
  added_block_modules: string[];
  unavailable_modules: string[];
  output_tree: string | null;
  run_systematics: boolean | null;
  save_cutbookkeepers: boolean | null;
  parse_error: string | null;
}

export interface AnalysisSummary {
  folder: string;
  path: string;
  web_url: string;
  status: AnalysisStatus;
  status_reason: string | null;
  configs: ConfigInfo[];
  extends: string | null;
  sample_lists: SampleListSummary[];
  runs: string[];
  campaigns: Record<string, number>;
  code_owners: string[];
  ntuple_lists: string[];
  warnings: string[];
}

export interface AnalysisList {
  ref: ResolvedRef;
  image: ImageStatus;
  analyses: AnalysisSummary[];
}

export interface AnalysisDetail {
  ref: ResolvedRef;
  image: ImageStatus;
  analysis: AnalysisSummary;
}

export interface DatasetRow {
  name: string;
  kind: "mc" | "data" | "unknown";
  project: string | null;
  dsid: string | null;
  physics_short: string | null;
  format: string | null;
  etag: string | null;
  simtag: string | null;
  rtags: string[];
  ptag: string | null;
  campaign: string;
  problem: string | null;
}

export interface SampleListDetail {
  ref: ResolvedRef;
  folder: string;
  summary: SampleListSummary;
  groups: { heading: string | null; datasets: DatasetRow[] }[];
}

export interface ConfigFile {
  ref: ResolvedRef;
  folder: string;
  config: ConfigInfo;
  content: string;
}

// ----- run setup -----------------------------------------------------------------------

export type Severity = "blocking" | "warning" | "info";

export interface RunSpec {
  analysis: string;
  ref: string;
  config: string;
  runs: string[];
  sample_lists: string[];
  systematics: "do" | "no" | "config-default";
  no_filter: boolean;
  no_thinning: boolean;
  include: string[];
  skip: string[];
  purpose: "production" | "test";
  memory_mb: number;
  files_per_job: number | null;
}

export interface Evidence {
  path: string;
  web_url: string | null;
  lines: string[];
  line_numbers: number[];
}

export interface Finding {
  id: string;
  code: string;
  severity: Severity;
  message: string;
  field: string | null;
  evidence: Evidence[];
}

export interface SelectionRow {
  run: string;
  sample: string;
  file: string;
  exists: boolean;
  datasets: number;
  selected: number;
  campaigns: Record<string, number>;
}

export interface ValidationReport {
  commit: string;
  ref_kind: "tag" | "branch" | "commit";
  findings: Finding[];
  selection: SelectionRow[];
}

export interface IssueCard {
  id: string;
  source: "reviewer" | "validator";
  severity: Severity;
  title: string;
  explanation: string;
  finding_codes: string[];
  evidence: Evidence[];
  patch: Partial<RunSpec> | null;
  patch_error: string | null;
  effect: { clears: string[]; introduces: string[] } | null;
  if_declined: string | null;
  owners: string[];
}

export interface DraftInfo {
  reasons: Record<string, string>;
  assumptions: string[];
  questions: string[];
  request: string | null;
  model: string | null;
  prompt_version: string | null;
}

export interface SetupView {
  id: string;
  status: "needs_input" | "ready";
  waiting: boolean;
  request: string | null;
  analysis_hint: string | null;
  resolved: ResolvedRef | null;
  spec: RunSpec | null;
  draft: DraftInfo | null;
  report: ValidationReport | null;
  issues: IssueCard[];
  errors: string[];
  models: Record<string, { model: string | null; prompt_version: string }>;
  history: { event: string; at: string; [key: string]: unknown }[];
  agents_enabled: boolean;
  blocking: number;
}

export interface AgentsStatus {
  enabled: boolean;
  base_url: string | null;
  builder_model: string | null;
  reviewer_model: string | null;
  structured_output: string;
  api_key_configured: boolean;
  reachable: boolean | null;
  detail: string | null;
}

export type SetupAction =
  | { action: "apply"; accept: string[]; dismiss: string[] }
  | { action: "edit"; spec: RunSpec }
  | { action: "redraft"; request: string }
  | { action: "finish" };

export interface DatasetQuery {
  folder: string;
  sample_list: string;
  include: string[];
  skip: string[];
  reason: string;
}

export interface SelectedDataset {
  name: string;
  campaign: string | null;
  source: string | null;
  line: number | null;
  reason: string;
}

export interface DatasetDraftInfo {
  queries: DatasetQuery[];
  extra_datasets: string[];
  assumptions: string[];
  questions: string[];
  request: string | null;
  model: string | null;
  prompt_version: string | null;
}

export interface RunRequestView {
  id: string;
  status: "needs_input" | "ready";
  waiting: boolean;
  request: string | null;
  analysis_hint: string | null;
  resolved: ResolvedRef | null;
  datasets: SelectedDataset[];
  draft: DatasetDraftInfo | null;
  receipt: { request_id: string; datasets: number; accepted_at: string } | null;
  errors: string[];
  history: { event: string; at: string; [key: string]: unknown }[];
  agents_enabled: boolean;
}

export type RequestAction =
  | { action: "edit"; datasets: string[] }
  | { action: "redraft"; request: string }
  | { action: "finish" };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function get<T>(path: string, params: Record<string, string | undefined> = {}): Promise<T> {
  const url = new URL(path, BACKEND_URL);
  for (const [key, value] of Object.entries(params)) {
    if (value) url.searchParams.set(key, value);
  }
  // The backend caches by commit SHA; always ask it so ref names resolve fresh
  return send<T>(url, { cache: "no-store" });
}

/** POSTs run the setup graph to its next pause, which can take a minute when a model is called. */
async function post<T>(path: string, body: unknown): Promise<T> {
  return send<T>(new URL(path, BACKEND_URL), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
}

async function send<T>(url: URL, init: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiError(503, `Cannot reach the backend at ${BACKEND_URL}`);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, detailText(body?.detail) ?? `Backend returned ${response.status}`);
  }
  return response.json() as Promise<T>;
}

/** FastAPI details are strings, or lists of validation errors. */
function detailText(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`).join("; ");
  }
  return undefined;
}

const seg = encodeURIComponent;

export const api = {
  status: () => get<ConnectionStatus>("/api/ewkit/status"),
  refs: () => get<RefList>("/api/ewkit/refs"),
  analyses: (ref?: string) => get<AnalysisList>("/api/ewkit/analyses", { ref }),
  analysis: (folder: string, ref?: string) => get<AnalysisDetail>(`/api/ewkit/analyses/${seg(folder)}`, { ref }),
  sampleList: (folder: string, name: string, ref?: string) =>
    get<SampleListDetail>(`/api/ewkit/analyses/${seg(folder)}/samples/${seg(name)}`, { ref }),
  configFile: (folder: string, name: string, ref?: string) =>
    get<ConfigFile>(`/api/ewkit/analyses/${seg(folder)}/configs/${seg(name)}`, { ref }),
  agents: (check = false) => get<AgentsStatus>("/api/setups/agents", { check: check ? "true" : undefined }),
  setup: (id: string) => get<SetupView>(`/api/setups/${seg(id)}`),
  createSetup: (body: { ref?: string; analysis?: string; request?: string; spec?: RunSpec }) =>
    post<SetupView>("/api/setups", body),
  setupAction: (id: string, action: SetupAction) => post<SetupView>(`/api/setups/${seg(id)}/actions`, action),
  request: (id: string) => get<RunRequestView>(`/api/requests/${seg(id)}`),
  createRequest: (body: { ref?: string; analysis?: string; request?: string }) =>
    post<RunRequestView>("/api/requests", body),
  requestAction: (id: string, action: RequestAction) =>
    post<RunRequestView>(`/api/requests/${seg(id)}/actions`, action),
};

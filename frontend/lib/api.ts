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

export interface SelectionSummary {
  count: number;
  by_run: Record<string, number>;
  by_sample: Record<string, number>;
  by_campaign: Record<string, number>;
  /** Scratch sample-list file the backend wrote, once there is a selection */
  path: string | null;
}

export interface SelectionMessage {
  role: "user" | "assistant";
  content: string;
}

export interface Selection {
  id: string;
  folder: string;
  sha: string;
  ref: string;
  selection: SelectionSummary;
  /** Everything in the analysis's sample lists that the agent can choose from */
  available?: SelectionSummary;
  messages: SelectionMessage[];
}

export interface SelectionReply {
  reply: string;
  selection: SelectionSummary;
  tool_calls: { name: string; args: Record<string, unknown> }[];
  mlflow_run_id: string | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function send<T>(url: URL, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    // The backend caches by commit SHA; always ask it so ref names resolve fresh
    response = await fetch(url, { cache: "no-store", ...init });
  } catch {
    throw new ApiError(503, `Cannot reach the backend at ${BACKEND_URL}`);
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(response.status, body?.detail ?? `Backend returned ${response.status}`);
  }
  return response.json() as Promise<T>;
}

async function get<T>(path: string, params: Record<string, string | undefined> = {}): Promise<T> {
  const url = new URL(path, BACKEND_URL);
  for (const [key, value] of Object.entries(params)) {
    if (value) url.searchParams.set(key, value);
  }
  return send<T>(url);
}

async function post<T>(path: string, body: unknown): Promise<T> {
  return send<T>(new URL(path, BACKEND_URL), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
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
  createSelection: (folder: string, ref: string | null) =>
    post<Selection>(`/api/ewkit/analyses/${seg(folder)}/selections`, { ref }),
  // The agent may take a couple of minutes to answer: it queries Rucio through MCP
  sendSelectionMessage: (folder: string, id: string, content: string) =>
    post<SelectionReply>(`/api/ewkit/analyses/${seg(folder)}/selections/${seg(id)}/messages`, { content }),
  selection: (folder: string, id: string) => get<Selection>(`/api/ewkit/analyses/${seg(folder)}/selections/${seg(id)}`),
};

/** Backend URL of a selection's sample-list file, which is plain text rather than JSON */
export const selectionDatasetsUrl = (folder: string, id: string) =>
  new URL(`/api/ewkit/analyses/${seg(folder)}/selections/${seg(id)}/datasets.txt`, BACKEND_URL);

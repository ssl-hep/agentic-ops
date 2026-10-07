import type { AgentsStatus } from "@/lib/api";

/** Shown when no model endpoint is configured: the form and validator still work without one. */
export function AgentsOffPanel({ status }: { status?: AgentsStatus | null }) {
  return (
    <section className="panel panel-info">
      <h2>Run setup agents are off</h2>
      <p>
        The builder and reviewer need an OpenAI-compatible model endpoint. Without one you can still fill in the run by
        hand, and the validator checks it the same way.
      </p>
      <p className="small">
        Set <span className="mono">LLM_BASE_URL</span> and <span className="mono">LLM_MODEL</span> on the backend (
        <span className="mono">llm.baseUrl</span> and <span className="mono">llm.model</span> in the Helm values), and put
        the token in the <span className="mono">agentic-ops-llm</span> Secret, as described under &ldquo;Model
        endpoint&rdquo; in the README.
        {status?.base_url && !status.enabled && (
          <>
            {" "}
            The base URL is set to <span className="mono">{status.base_url}</span>, but no model is.
          </>
        )}
      </p>
    </section>
  );
}

export function AgentsLine({ status }: { status: AgentsStatus }) {
  const models =
    status.builder_model === status.reviewer_model
      ? status.builder_model
      : `${status.builder_model} (builder), ${status.reviewer_model} (reviewer)`;
  const tone = status.reachable === false ? "dot-bad" : status.reachable ? "dot-ok" : "dot-unknown";
  return (
    <p className="small muted">
      <span className={`dot ${tone}`} aria-hidden /> Agents use <span className="mono">{models}</span> at{" "}
      <span className="mono">{status.base_url}</span>
      {status.reachable === false && <span className="bad-text"> · not reachable: {status.detail}</span>}
    </p>
  );
}

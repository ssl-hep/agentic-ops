import { AgentsLine, AgentsOffPanel } from "@/components/AgentsPanel";
import { SubmitButton } from "@/components/SubmitButton";
import { api } from "@/lib/api";
import { param } from "@/lib/format";
import { createSetup } from "../actions";

const EXAMPLE = "Run 3 background and data for RadiativeDecays at EFP_v6.6, with systematics";

export default async function NewSetupPage(props: PageProps<"/setups/new">) {
  const searchParams = await props.searchParams;
  const error = param(searchParams, "error");
  const [agents, analyses, refs] = await Promise.all([
    api.agents(true).catch(() => null),
    api.analyses().catch(() => null),
    api.refs().catch(() => null),
  ]);
  const enabled = agents?.enabled ?? false;

  return (
    <>
      <header className="page-head">
        <h1>New run</h1>
        <p className="muted">
          Describe the production in words and the builder agent drafts the run options, or pick an analysis and fill
          them in yourself. Either way the validator checks the options at the pinned commit and the reviewer explains
          what it found. Nothing is submitted from here.
        </p>
      </header>
      {error && (
        <p className="panel panel-error" role="alert">
          {error}
        </p>
      )}
      {agents && enabled ? <AgentsLine status={agents} /> : <AgentsOffPanel status={agents} />}

      <form action={createSetup} className="panel new-setup">
        {enabled && (
          <label className="block">
            <span className="field-label">Request</span>
            <textarea
              name="request"
              rows={3}
              defaultValue={param(searchParams, "request") ?? ""}
              placeholder={EXAMPLE}
            />
          </label>
        )}
        <div className="filters">
          <label>
            Analysis{" "}
            <select name="analysis" defaultValue={param(searchParams, "analysis") ?? ""} required={!enabled}>
              <option value="">{enabled ? "let the builder choose" : "choose…"}</option>
              {analyses?.analyses.map((a) => (
                <option key={a.folder} value={a.folder}>
                  {a.folder}
                  {a.status !== "runnable" ? ` (${a.status.replace("_", " ")})` : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            EWKit ref{" "}
            <input
              name="ref"
              list="new-ref-options"
              defaultValue={param(searchParams, "ref") ?? ""}
              placeholder={refs?.default_branch ?? "default branch"}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <datalist id="new-ref-options">
            {refs?.tags.map((t) => (
              <option key={t.name} value={t.name} />
            ))}
            {refs?.branches.map((b) => (
              <option key={b.name} value={b.name} />
            ))}
          </datalist>
          <SubmitButton pendingText={enabled ? "Drafting and reviewing…" : "Checking…"}>
            {enabled ? "Draft the run" : "Set up the run"}
          </SubmitButton>
        </div>
        {enabled && (
          <p className="small muted">
            The request, the RunSpec, dataset names and config excerpts are sent to the model endpoint above. Leave the
            request empty to start from the analysis&rsquo;s defaults instead.
          </p>
        )}
      </form>
    </>
  );
}

import { AgentsLine, AgentsOffPanel } from "@/components/AgentsPanel";
import { SubmitButton } from "@/components/SubmitButton";
import { api } from "@/lib/api";
import { param } from "@/lib/format";
import { createRequest } from "../actions";

const EXAMPLE = "Run 3 Z+jets background and data for RadiativeDecays at EFP_v6.6";

export default async function NewRequestPage(props: PageProps<"/requests/new">) {
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
          Say what data the run should read. The builder agent picks the sample lists and patterns, code expands them
          into the dataset list, and you check and edit it before marking it ready. Nothing is submitted from here.
        </p>
      </header>
      {error && (
        <p className="panel panel-error" role="alert">
          {error}
        </p>
      )}
      {agents && enabled ? <AgentsLine status={agents} /> : <AgentsOffPanel status={agents} />}

      <form action={createRequest} className="panel new-setup">
        {enabled && (
          <label className="block">
            <span className="field-label">What data should the run read?</span>
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
            <select name="analysis" defaultValue={param(searchParams, "analysis") ?? ""}>
              <option value="">{enabled ? "let the builder choose" : "none"}</option>
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
          <SubmitButton pendingText={enabled ? "Finding the datasets…" : "Starting…"}>
            {enabled ? "Find the datasets" : "List datasets by hand"}
          </SubmitButton>
        </div>
        {enabled && (
          <p className="small muted">
            The request, folder and sample list names, and dataset names are sent to the model endpoint above. Leave the
            request empty to list the datasets yourself.
          </p>
        )}
      </form>
    </>
  );
}

import Link from "next/link";
import { AgentsOffPanel } from "@/components/AgentsPanel";
import { ErrorPanel } from "@/components/ErrorPanel";
import { SubmitButton } from "@/components/SubmitButton";
import { api, type RunRequestView } from "@/lib/api";
import { analysisHref, fmtDate, param } from "@/lib/format";
import { editDatasets, finishRequest, redraftRequest } from "../actions";

export default async function RequestPage(props: PageProps<"/requests/[id]">) {
  const { id } = await props.params;
  const searchParams = await props.searchParams;
  const error = param(searchParams, "error");

  let run: RunRequestView;
  try {
    run = await api.request(id);
  } catch (err) {
    return <ErrorPanel error={err} />;
  }
  const editable = run.waiting;
  const agentsOn = run.agents_enabled;
  const gitRef = run.resolved?.ref;
  const sourceLink = (source: string) => {
    const [folder, list] = source.split("/");
    return analysisHref(folder, gitRef, { list });
  };

  return (
    <>
      <nav className="crumbs">
        <Link href="/requests/new">New run</Link> / <span>Run request</span>
      </nav>
      <header className="page-head">
        <h1>
          Run request{" "}
          {run.status === "ready" ? (
            <span className="badge badge-runnable">Spec ready</span>
          ) : (
            <span className="badge badge-config_only">Needs input</span>
          )}
        </h1>
        {run.resolved && (
          <p className="muted small">
            EWKit <span className="mono">{run.resolved.ref}</span> at{" "}
            <a href={run.resolved.web_url} target="_blank" rel="noreferrer" className="mono">
              {run.resolved.short_sha}
            </a>{" "}
            {run.resolved.title} · {fmtDate(run.resolved.committed_date)}
          </p>
        )}
      </header>

      {error && (
        <p className="panel panel-error" role="alert">
          {error}
        </p>
      )}
      {run.errors.length > 0 && (
        <section className="panel panel-warn" role="alert">
          <h2>Problems</h2>
          {run.errors.map((e) => (
            <p key={e}>{e}</p>
          ))}
          <p className="small">Edit the list below, or change the request and redraft.</p>
        </section>
      )}
      {!agentsOn && editable && <AgentsOffPanel />}

      {(run.request || (agentsOn && editable)) && (
        <section className="panel request">
          <h2>Request</h2>
          {run.request && <p className="request-text">{run.request}</p>}
          {run.draft && (run.draft.assumptions.length > 0 || run.draft.questions.length > 0) && (
            <div className="builder-notes">
              {run.draft.assumptions.length > 0 && (
                <div>
                  <h3>Builder&rsquo;s assumptions</h3>
                  <ul>
                    {run.draft.assumptions.map((a) => (
                      <li key={a}>{a}</li>
                    ))}
                  </ul>
                </div>
              )}
              {run.draft.questions.length > 0 && (
                <div>
                  <h3>Questions for you</h3>
                  <ul>
                    {run.draft.questions.map((q) => (
                      <li key={q}>{q}</li>
                    ))}
                  </ul>
                  <p className="small muted">Answer by editing the list, or add the answer to the request and redraft.</p>
                </div>
              )}
            </div>
          )}
          {agentsOn && editable && (
            <form action={redraftRequest} className="redraft">
              <input type="hidden" name="id" value={run.id} />
              <textarea
                name="request"
                rows={2}
                defaultValue={run.request ?? ""}
                required
                placeholder="What data should the run read?"
              />
              <SubmitButton className="secondary" pendingText="Finding the datasets…">
                {run.request ? "Redraft" : "Draft from request"}
              </SubmitButton>
            </form>
          )}
          {run.draft && (
            <p className="small muted">
              Drafted by <span className="mono">{run.draft.model}</span> ({run.draft.prompt_version}). Redrafting
              replaces the list with a new draft.
            </p>
          )}
        </section>
      )}

      {run.draft && run.draft.queries.length > 0 && (
        <section>
          <h2>How the builder chose them</h2>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Sample list</th>
                  <th scope="col">Include</th>
                  <th scope="col">Skip</th>
                  <th scope="col">Why</th>
                </tr>
              </thead>
              <tbody>
                {run.draft.queries.map((q, i) => (
                  <tr key={i}>
                    <td>
                      <Link href={sourceLink(`${q.folder}/${q.sample_list}`)} className="mono">
                        {q.folder}/{q.sample_list}
                      </Link>
                    </td>
                    <td className="mono">{q.include.join(" ") || <span className="muted">all</span>}</td>
                    <td className="mono">{q.skip.join(" ") || <span className="muted">none</span>}</td>
                    <td>{q.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section>
        <h2>Datasets ({run.datasets.length})</h2>
        {run.datasets.length === 0 ? (
          <p className="muted">No datasets yet.{editable && " Paste names below, one per line."}</p>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Dataset</th>
                  <th scope="col">Campaign</th>
                  <th scope="col">From</th>
                  <th scope="col">Why</th>
                </tr>
              </thead>
              <tbody>
                {run.datasets.map((d) => (
                  <tr key={d.name}>
                    <td className="mono small">{d.name}</td>
                    <td>{d.campaign ?? <span className="muted">–</span>}</td>
                    <td className="small">
                      {d.source ? (
                        <Link href={sourceLink(d.source)} className="mono">
                          {d.source}
                          {d.line ? `:${d.line}` : ""}
                        </Link>
                      ) : (
                        <span className="muted">named directly</span>
                      )}
                    </td>
                    <td className="small">{d.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {editable && (
          <form action={editDatasets} className="redraft">
            <input type="hidden" name="id" value={run.id} />
            <textarea
              name="datasets"
              rows={Math.min(Math.max(run.datasets.length + 1, 4), 16)}
              defaultValue={run.datasets.map((d) => d.name).join("\n")}
              spellCheck={false}
              className="mono small"
              aria-label="Dataset names, one per line"
            />
            <SubmitButton className="secondary" pendingText="Saving…">
              Save the list
            </SubmitButton>
          </form>
        )}
      </section>

      <section className="panel finish">
        {run.status === "ready" && run.receipt ? (
          <p>
            <strong>The spec is ready.</strong> Handed to the Spec-is-ready service at {fmtDate(run.receipt.accepted_at)}{" "}
            ({run.receipt.datasets} dataset{run.receipt.datasets === 1 ? "" : "s"}). The next stages (Rucio checks,
            Gate 1, test jobs) are not built yet.
          </p>
        ) : (
          <form action={finishRequest}>
            <input type="hidden" name="id" value={run.id} />
            <SubmitButton disabled={run.datasets.length === 0} pendingText="Handing over…">
              Spec is ready
            </SubmitButton>{" "}
            <span className="small muted">
              Hands this dataset list to the next stage. It cannot be edited afterwards.
            </span>
          </form>
        )}
      </section>

      <section>
        <h2>History</h2>
        <ol className="history small">
          {run.history.map((h, i) => (
            <li key={i}>
              <span className="muted">{fmtDate(h.at)}</span> {h.event}
              {typeof h.datasets === "number" && <span className="muted"> · {h.datasets} datasets</span>}
              {typeof h.model === "string" && <span className="muted"> · {h.model}</span>}
              {typeof h.step === "string" && <span className="bad-text"> · {h.step}</span>}
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}

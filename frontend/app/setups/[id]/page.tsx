import Link from "next/link";
import { AgentsOffPanel } from "@/components/AgentsPanel";
import { ErrorPanel } from "@/components/ErrorPanel";
import { FindingList, SelectionTable } from "@/components/FindingList";
import { IssueCard } from "@/components/IssueCard";
import { RunSpecForm } from "@/components/RunSpecForm";
import { SubmitButton } from "@/components/SubmitButton";
import { api, type AnalysisSummary, type RunSpec, type SetupView } from "@/lib/api";
import { analysisHref, fmtDate, param } from "@/lib/format";
import { finish, redraft } from "../actions";

const BLANK_SPEC: Omit<RunSpec, "analysis" | "ref"> = {
  config: "config.yaml",
  runs: [],
  sample_lists: [],
  systematics: "config-default",
  no_filter: false,
  no_thinning: false,
  include: [],
  skip: [],
  purpose: "test",
  memory_mb: 2000,
  files_per_job: null,
};

export default async function SetupPage(props: PageProps<"/setups/[id]">) {
  const { id } = await props.params;
  const searchParams = await props.searchParams;
  const error = param(searchParams, "error");
  const prefill = param(searchParams, "prefill");

  let setup: SetupView;
  try {
    setup = await api.setup(id);
  } catch (err) {
    return <ErrorPanel error={err} />;
  }
  const sha = setup.resolved?.sha;
  const [analysis, analyses] = await Promise.all([
    setup.spec?.analysis && sha
      ? api.analysis(setup.spec.analysis, sha).then((d) => d.analysis).catch(() => null)
      : Promise.resolve(null as AnalysisSummary | null),
    sha ? api.analyses(sha).catch(() => null) : Promise.resolve(null),
  ]);

  const editable = setup.waiting;
  const agentsOn = setup.agents_enabled;
  const pending = agentsOn ? "Checking and reviewing…" : "Checking…";
  const baseSpec: RunSpec = setup.spec ?? {
    ...BLANK_SPEC,
    analysis: setup.analysis_hint ?? "",
    ref: setup.resolved?.ref ?? "",
  };
  const prefillCard = prefill ? setup.issues.find((c) => c.id === prefill) : undefined;
  const formSpec = prefillCard?.patch ? { ...baseSpec, ...prefillCard.patch } : baseSpec;
  const findings = setup.report?.findings ?? [];
  const title = setup.spec?.analysis || setup.analysis_hint;

  return (
    <>
      <nav className="crumbs">
        <Link href="/setups/new">New run</Link> / <span>Run setup</span>
      </nav>
      <header className="page-head">
        <h1>
          {title ? (
            <>
              Run setup: <Link href={analysisHref(title, setup.resolved?.ref)}>{title}</Link>
            </>
          ) : (
            "Run setup"
          )}{" "}
          {setup.status === "ready" ? (
            <span className="badge badge-runnable">Spec ready</span>
          ) : (
            <span className="badge badge-config_only">Needs input</span>
          )}
        </h1>
        {setup.resolved && (
          <p className="muted small">
            <span className="mono">{setup.resolved.ref}</span>
            {setup.report && <> ({setup.report.ref_kind})</>} at{" "}
            <a href={setup.resolved.web_url} target="_blank" rel="noreferrer" className="mono">
              {setup.resolved.short_sha}
            </a>{" "}
            {setup.resolved.title} · {fmtDate(setup.resolved.committed_date)}
          </p>
        )}
      </header>

      {error && (
        <p className="panel panel-error" role="alert">
          {error}
        </p>
      )}
      {setup.errors.length > 0 && (
        <section className="panel panel-error" role="alert">
          <h2>Something went wrong</h2>
          {setup.errors.map((e) => (
            <p key={e}>{e}</p>
          ))}
          <p className="small">The setup kept its last good state. Edit the spec, or try the request again.</p>
        </section>
      )}
      {!agentsOn && editable && <AgentsOffPanel />}

      {(setup.request || (agentsOn && editable)) && (
        <section className="panel request">
          <h2>Request</h2>
          {setup.request && <p className="request-text">{setup.request}</p>}
          {setup.draft && (setup.draft.assumptions.length > 0 || setup.draft.questions.length > 0) && (
            <div className="builder-notes">
              {setup.draft.assumptions.length > 0 && (
                <div>
                  <h3>Builder&rsquo;s assumptions</h3>
                  <ul>
                    {setup.draft.assumptions.map((a) => (
                      <li key={a}>{a}</li>
                    ))}
                  </ul>
                </div>
              )}
              {setup.draft.questions.length > 0 && (
                <div>
                  <h3>Questions for you</h3>
                  <ul>
                    {setup.draft.questions.map((q) => (
                      <li key={q}>{q}</li>
                    ))}
                  </ul>
                  <p className="small muted">Answer by editing the form, or add the answer to the request and redraft.</p>
                </div>
              )}
            </div>
          )}
          {agentsOn && editable && (
            <form action={redraft} className="redraft">
              <input type="hidden" name="id" value={setup.id} />
              <textarea name="request" rows={2} defaultValue={setup.request ?? ""} required placeholder="Describe the run" />
              <SubmitButton className="secondary" pendingText="Drafting…">
                {setup.request ? "Redraft" : "Draft from request"}
              </SubmitButton>
            </form>
          )}
          {setup.draft && (
            <p className="small muted">
              Drafted by <span className="mono">{setup.draft.model}</span> ({setup.draft.prompt_version}). Redrafting
              replaces the form with a new draft.
            </p>
          )}
        </section>
      )}

      <div className="setup-grid">
        <section id="spec">
          <h2>Run options</h2>
          {prefillCard && (
            <p className="panel panel-info small">
              The form holds the patch from &ldquo;{prefillCard.title}&rdquo;. Change it if needed, then save.
            </p>
          )}
          <RunSpecForm
            key={prefill ?? "spec"}
            setupId={setup.id}
            spec={formSpec}
            analysis={analysis}
            folders={analyses?.analyses.map((a) => a.folder) ?? []}
            reasons={setup.draft?.reasons ?? {}}
            findings={findings}
            disabled={!editable}
            pendingText={pending}
          />
        </section>

        <div>
          {setup.issues.length > 0 && (
            <section>
              <h2>Reviewer</h2>
              <div className="issue-list">
                {setup.issues.map((card) => (
                  <IssueCard key={card.id} card={card} spec={setup.spec} setupId={setup.id} canAct={editable} />
                ))}
              </div>
            </section>
          )}
          {agentsOn && setup.report && setup.issues.length === 0 && setup.models.reviewer && (
            <p className="muted">
              No open reviewer cards
              {setup.blocking > 0 ? "; the blocking findings below still need a change to the spec." : " for this spec."}
            </p>
          )}

          {setup.report && (
            <section>
              <h2>Input checks</h2>
              <p className="small muted">
                Deterministic checks of the spec at <span className="mono">{setup.report.commit.slice(0, 8)}</span>. Rucio,
                image and <span className="mono">-PS</span> checks come with Gate 1.
              </p>
              <FindingList findings={findings} />
              <SelectionTable rows={setup.report.selection} />
            </section>
          )}

          <section className="panel finish">
            {setup.status === "ready" ? (
              <p>
                <strong>The spec is ready.</strong> The next stages (Rucio and image checks, the plan estimate at Gate 1,
                test jobs) are not built yet.
              </p>
            ) : (
              <form action={finish}>
                <input type="hidden" name="id" value={setup.id} />
                <SubmitButton disabled={!setup.spec || !setup.report || setup.blocking > 0} pendingText="Saving…">
                  Spec is ready
                </SubmitButton>{" "}
                <span className="small muted">
                  {setup.blocking > 0
                    ? `${setup.blocking} blocking finding${setup.blocking > 1 ? "s" : ""} left.`
                    : "Marks this spec as the one to check against Rucio and take to Gate 1. It cannot be edited afterwards."}
                </span>
              </form>
            )}
          </section>
        </div>
      </div>

      <section>
        <h2>History</h2>
        <ol className="history small">
          {setup.history.map((h, i) => (
            <li key={i}>
              <span className="muted">{fmtDate(h.at)}</span> {h.event}
              {typeof h.title === "string" && <> &ldquo;{h.title}&rdquo;</>}
              {typeof h.model === "string" && <span className="muted"> · {h.model}</span>}
              {typeof h.step === "string" && <span className="bad-text"> · {h.step}</span>}
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}

"use client";

import { startTransition, useActionState, useState, type FormEvent } from "react";
import type { ActiveSelection } from "@/components/DatasetAgentPanel";
import type { AnalysisSummary, ImageStatus, ResolvedRef } from "@/lib/api";
import { fmt } from "@/lib/format";
import { QUEUES, RUN_DEFAULTS, type RunFormState } from "@/lib/run";

type SubmitRun = (prev: RunFormState, form: FormData) => Promise<RunFormState>;

/**
 * Run parameters for one analysis at one commit, with a live count of what is selected. An active
 * agent selection replaces the sample-list and campaign choices.
 */
export function RunForm({
  analysis: a,
  resolved,
  image,
  submitRun,
  selection,
}: {
  analysis: AnalysisSummary;
  resolved: ResolvedRef;
  image: ImageStatus;
  submitRun: SubmitRun;
  selection: ActiveSelection | null;
}) {
  const [state, formAction, pending] = useActionState(submitRun, { status: "idle" });
  const campaigns = Object.keys(a.campaigns);
  const [lists, setLists] = useState(() => a.sample_lists.filter((s) => s.datasets > 0).map((s) => s.name));
  const [chosen, setChosen] = useState(() => campaigns.filter((c) => c !== "unknown"));
  const [testJob, setTestJob] = useState(RUN_DEFAULTS.test_job);

  const selectedLists = a.sample_lists.filter((s) => lists.includes(s.name));
  const perCampaign = (c: string) => selectedLists.reduce((n, s) => n + (s.campaigns[c] ?? 0), 0);
  const datasets = chosen.reduce((n, c) => n + perCampaign(c), 0);
  const usable = a.configs.filter((c) => !c.parse_error);
  const defaultConfig = (usable.find((c) => c.unavailable_modules.length === 0) ?? usable[0])?.name;
  const blocker = a.status === "runnable" ? null : (a.status_reason ?? "This analysis cannot run as it is.");

  function onChange(event: FormEvent<HTMLFormElement>) {
    const form = new FormData(event.currentTarget);
    // Disabled checkboxes drop out of the form data; keep the choices for when the selection is turned off
    if (!selection) {
      setLists(form.getAll("sample_lists").map(String));
      setChosen(form.getAll("campaigns").map(String));
    }
    setTestJob(form.get("test_job") === "on");
  }

  // Submit through a transition rather than <form action>, which would reset every field afterwards
  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    startTransition(() => formAction(form));
  }

  return (
    <form className="run-form" onChange={onChange} onSubmit={onSubmit}>
      <input type="hidden" name="folder" value={a.folder} />
      <input type="hidden" name="ref" value={resolved.ref} />
      <input type="hidden" name="sha" value={resolved.sha} />
      {selection && <input type="hidden" name="selection_id" value={selection.id} />}

      <fieldset>
        <legend>Inputs</legend>
        <div className="field">
          <span className="field-label">Pinned to</span>
          <span>
            <span className="mono">{resolved.short_sha}</span> <span className="muted">({resolved.ref})</span>
          </span>
        </div>
        <div className="field">
          <span className="field-label">Image</span>
          <span className={image.available ? "mono small" : "warn-text small"}>
            {image.available ? image.location : `No CI image for this ref. ${image.detail ?? ""}`}
          </span>
        </div>
        <label className="field">
          <span className="field-label">Config</span>
          <select name="config" defaultValue={defaultConfig} required>
            {a.configs.map((c) => (
              <option key={c.name} value={c.name} disabled={!!c.parse_error}>
                {c.name}
                {c.parse_error ? " (does not parse)" : c.unavailable_modules.length > 0 ? " (needs modules not in the image)" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field-label">Output name</span>
          <input name="output_name" defaultValue={`${a.folder}.${resolved.short_sha}`} required spellCheck={false} />
        </label>
      </fieldset>

      <fieldset>
        <legend>Samples</legend>
        {a.sample_lists.length === 0 ? (
          <p className="muted">
            No sample lists in <span className="mono">DAODs/</span>.
          </p>
        ) : (
          <div className="checks">
            {a.sample_lists.map((s) => (
              <label key={s.name} className={s.datasets === 0 ? "faint" : undefined}>
                <input
                  type="checkbox"
                  name="sample_lists"
                  value={s.name}
                  defaultChecked={s.datasets > 0}
                  disabled={s.datasets === 0 || !!selection}
                />{" "}
                <span className="mono">{s.name}</span>{" "}
                <span className="muted small">{s.datasets === 0 ? "empty" : `${fmt(s.datasets)} dataset${s.datasets === 1 ? "" : "s"}`}</span>
              </label>
            ))}
          </div>
        )}
        <p className="field-label">Campaigns</p>
        <div className="checks checks-row">
          {campaigns.map((c) => (
            <label key={c} className={c === "unknown" ? "warn-text" : undefined}>
              <input
                type="checkbox"
                name="campaigns"
                value={c}
                defaultChecked={c !== "unknown"}
                disabled={!!selection}
              />{" "}
              <span className="mono">{c}</span> <span className="muted small">{fmt(perCampaign(c))}</span>
            </label>
          ))}
        </div>
        {selection ? (
          <p className="small info-text">
            <span className="strong">{fmt(selection.count)}</span> dataset{selection.count === 1 ? "" : "s"} from agent
            selection
          </p>
        ) : (
          <p className="small">
            <span className="strong">{fmt(datasets)}</span> dataset{datasets === 1 ? "" : "s"} selected
          </p>
        )}
      </fieldset>

      <fieldset>
        <legend>Jobs</legend>
        <div className="field">
          <span className="field-label">Queue</span>
          <span className="checks checks-row">
            {Object.entries(QUEUES).map(([value, label]) => (
              <label key={value}>
                <input type="radio" name="queue" value={value} defaultChecked={value === RUN_DEFAULTS.queue} /> {label}
              </label>
            ))}
          </span>
        </div>
        <label className="field">
          <span className="field-label">Overflow</span>
          <span>
            <input type="checkbox" name="allow_mwt2_overflow" defaultChecked={RUN_DEFAULTS.allow_mwt2_overflow} /> Allow
            jobs to overflow to MWT2
          </span>
        </label>
        <label className="field">
          <span className="field-label">Files per job</span>
          <input type="number" name="files_per_job" min={1} defaultValue={RUN_DEFAULTS.files_per_job} required />
        </label>
        <label className="field">
          <span className="field-label">Memory per job</span>
          <span>
            <input type="number" name="memory_gb" min={1} defaultValue={RUN_DEFAULTS.memory_gb} required /> GB
          </span>
        </label>
        <label className="field">
          <span className="field-label">Events per job</span>
          <input type="number" name="max_events" min={1} placeholder="all" />
        </label>
      </fieldset>

      <fieldset>
        <legend>Test job and limits</legend>
        <label className="field">
          <span className="field-label">Test job</span>
          <span>
            <input type="checkbox" name="test_job" defaultChecked={RUN_DEFAULTS.test_job} /> Run one short job per
            sample list first
          </span>
        </label>
        <label className="field">
          <span className="field-label">Test job events</span>
          <input
            type="number"
            name="test_events"
            min={1}
            defaultValue={RUN_DEFAULTS.test_events}
            disabled={!testJob}
            required
          />
        </label>
        <label className="field">
          <span className="field-label">Job cap</span>
          <input type="number" name="max_jobs" min={1} defaultValue={RUN_DEFAULTS.max_jobs} required />
        </label>
        <label className="field">
          <span className="field-label">Core-hour cap</span>
          <input type="number" name="max_core_hours" min={1} defaultValue={RUN_DEFAULTS.max_core_hours} required />
        </label>
        <label className="field field-wide">
          <span className="field-label">Notes</span>
          <textarea name="notes" rows={3} placeholder="Why this run, what changed since the last one…" />
        </label>
      </fieldset>

      <div className="run-actions">
        <button type="submit" disabled={pending || !!blocker}>
          {pending ? "Checking…" : "Submit run"}
        </button>
        {blocker ? (
          <span className="small bad-text">{blocker}</span>
        ) : (
          <span className="small muted">Submission is not connected yet; nothing will run.</span>
        )}
      </div>

      {state.status === "invalid" && (
        <ul className="panel panel-error warnings" role="alert">
          {state.errors.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
      {state.status === "noop" && (
        <section className="panel panel-info" role="status">
          <h2>Not submitted</h2>
          <p>Run submission is not connected yet. This is the request it would send:</p>
          <pre className="code">{JSON.stringify(state.request, null, 2)}</pre>
        </section>
      )}
    </form>
  );
}

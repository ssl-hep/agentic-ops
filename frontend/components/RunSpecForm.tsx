import type { ReactNode } from "react";
import { editSpec } from "@/app/setups/actions";
import type { AnalysisSummary, Finding, RunSpec } from "@/lib/api";
import { fmt } from "@/lib/format";
import { SubmitButton } from "./SubmitButton";

const union = (...lists: string[][]) => [...new Set(lists.flat())];

/**
 * The RunSpec as an editable form (D28): the one editor of what will run. The builder only fills
 * it in; saving sends the whole spec back through the validator and the reviewer.
 */
export function RunSpecForm({
  setupId,
  spec,
  analysis,
  folders,
  reasons,
  findings,
  disabled,
  pendingText,
}: {
  setupId: string;
  spec: RunSpec;
  analysis: AnalysisSummary | null;
  folders: string[];
  reasons: Record<string, string>;
  findings: Finding[];
  disabled: boolean;
  pendingText: string;
}) {
  const flagged = new Set(findings.filter((f) => f.severity !== "info").map((f) => f.field));
  const configs = union(analysis?.configs.map((c) => c.name) ?? [], [spec.config]);
  const runs = union(analysis?.runs ?? [], spec.runs).sort();
  const samples = union([...new Set(analysis?.sample_lists.map((s) => s.sample) ?? [])].sort(), spec.sample_lists);
  const config = analysis?.configs.find((c) => c.name === spec.config);
  const count = (sample: string) =>
    analysis?.sample_lists
      .filter((s) => s.sample === sample)
      .map((s) => `${s.run} ${fmt(s.datasets)}`)
      .join(" · ");

  const about = (name: keyof RunSpec) => ({ flagged: flagged.has(name), reason: reasons[name] });

  return (
    <form action={editSpec} className="spec-form">
      <input type="hidden" name="id" value={setupId} />
      <fieldset disabled={disabled}>
        <Field {...about("analysis")} label="Analysis">
          <select name="analysis" defaultValue={spec.analysis}>
            {union(folders, [spec.analysis]).map((f) => (
              <option key={f}>{f}</option>
            ))}
          </select>
        </Field>
        <Field {...about("ref")} label="EWKit ref">
          <input name="ref" defaultValue={spec.ref} spellCheck={false} required />
        </Field>
        <Field {...about("purpose")} label="Purpose">
          <label>
            <input type="radio" name="purpose" value="production" defaultChecked={spec.purpose === "production"} />{" "}
            production <span className="muted small">(needs a tag)</span>
          </label>
          <label>
            <input type="radio" name="purpose" value="test" defaultChecked={spec.purpose === "test"} /> test
          </label>
        </Field>
        <Field {...about("config")} label="Config">
          <select name="config" defaultValue={spec.config}>
            {configs.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </select>
        </Field>
        <Field {...about("runs")} label="Runs">
          {runs.map((run) => (
            <label key={run}>
              <input type="checkbox" name="runs" value={run} defaultChecked={spec.runs.includes(run)} /> {run}
            </label>
          ))}
          {runs.length === 0 && <span className="faint">no sample lists</span>}
        </Field>
        <Field {...about("sample_lists")} label="Sample lists">
          {samples.map((sample) => (
            <label key={sample} className="block">
              <input type="checkbox" name="sample_lists" value={sample} defaultChecked={spec.sample_lists.includes(sample)} />{" "}
              <span className="mono">{sample}</span> <span className="small muted">{count(sample)}</span>
            </label>
          ))}
        </Field>
        <Field {...about("systematics")} label="Systematics">
          {(["config-default", "do", "no"] as const).map((value) => (
            <label key={value} className="block">
              <input type="radio" name="systematics" value={value} defaultChecked={spec.systematics === value} />{" "}
              {value === "config-default" ? (
                <>
                  config default{" "}
                  {config && (
                    <span className="small muted">(runSystematics: {String(config.run_systematics ?? "not set")})</span>
                  )}
                </>
              ) : (
                <span className="mono">--{value}Sys</span>
              )}
            </label>
          ))}
        </Field>
        <Field {...about("no_filter")} label="Flags">
          <label className="block">
            <input type="checkbox" name="no_filter" defaultChecked={spec.no_filter} /> <span className="mono">--noFilter</span>
          </label>
          <label className="block">
            <input type="checkbox" name="no_thinning" defaultChecked={spec.no_thinning} />{" "}
            <span className="mono">--noThinning</span>
          </label>
        </Field>
        <Field {...about("include")} label="Include (-w)">
          <textarea name="include" rows={2} defaultValue={spec.include.join("\n")} placeholder="one regex per line" spellCheck={false} />
        </Field>
        <Field {...about("skip")} label="Skip (--skip)">
          <textarea name="skip" rows={2} defaultValue={spec.skip.join("\n")} placeholder="one regex per line" spellCheck={false} />
        </Field>
        <Field {...about("memory_mb")} label="Memory (MB)">
          <input type="number" name="memory_mb" defaultValue={spec.memory_mb} min={500} max={64000} step={100} />
        </Field>
        <Field {...about("files_per_job")} label="Files per job">
          <input
            type="number"
            name="files_per_job"
            defaultValue={spec.files_per_job ?? ""}
            min={1}
            placeholder="auto, at Gate 1"
          />
        </Field>
        <div className="form-actions">
          <SubmitButton pendingText={pendingText}>Save and check</SubmitButton>
        </div>
      </fieldset>
    </form>
  );
}

function Field({
  label,
  flagged,
  reason,
  children,
}: {
  label: string;
  flagged: boolean;
  reason?: string;
  children: ReactNode;
}) {
  return (
    <div className={flagged ? "field field-flagged" : "field"} role="group" aria-label={label}>
      <div className="field-label" aria-hidden>
        {label}
      </div>
      <div>
        {children}
        {reason && <div className="small muted reason">Builder: {reason}</div>}
      </div>
    </div>
  );
}

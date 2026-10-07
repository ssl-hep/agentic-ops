import type { Evidence, Finding, SelectionRow, Severity } from "@/lib/api";
import { fmt } from "@/lib/format";
import { CampaignChips } from "./CampaignChips";

const SEVERITY_LABELS: Record<Severity, string> = { blocking: "Blocking", warning: "Warning", info: "Info" };

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <span className={`badge badge-${severity}`}>{SEVERITY_LABELS[severity]}</span>;
}

/** Files and lines a finding or card points at, linked at the pinned commit. */
export function EvidenceList({ evidence }: { evidence: Evidence[] }) {
  if (evidence.length === 0) return null;
  return (
    <ul className="evidence">
      {evidence.map((e, i) => (
        <li key={`${e.path}-${i}`}>
          {e.web_url ? (
            <a href={e.web_url} target="_blank" rel="noreferrer" className="mono small">
              {e.path}
            </a>
          ) : (
            <span className="mono small">{e.path}</span>
          )}
          {e.lines.length > 0 && (
            <pre className="evidence-lines">
              {e.lines.map((line, j) => (
                <div key={j}>
                  {e.line_numbers[j] ? <span className="faint">{String(e.line_numbers[j]).padStart(4)} </span> : null}
                  {line}
                </div>
              ))}
            </pre>
          )}
        </li>
      ))}
    </ul>
  );
}

/** The validator's findings, blocking first. These are code's verdicts, not the reviewer's. */
export function FindingList({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) {
    return <p className="muted">No findings: every check the validator runs passed.</p>;
  }
  return (
    <div className="table-scroll">
      <table className="findings">
        <tbody>
          {findings.map((f) => (
            <tr key={f.id}>
              <td>
                <SeverityBadge severity={f.severity} />
              </td>
              <td>
                <div className="mono small strong">{f.code}</div>
                {f.field && <div className="small muted">{f.field}</div>}
              </td>
              <td>
                {f.message}
                <EvidenceList evidence={f.evidence} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Which sample lists the spec selects, and what the include/skip patterns leave of them. */
export function SelectionTable({ rows }: { rows: SelectionRow[] }) {
  if (rows.length === 0) return null;
  const total = rows.reduce((n, r) => n + r.selected, 0);
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th scope="col">List</th>
            <th scope="col" className="num">
              Datasets
            </th>
            <th scope="col" className="num">
              Selected
            </th>
            <th scope="col">Campaigns selected</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.file}>
              <td className="mono small">{r.exists ? r.file : <span className="faint">{r.file} (missing)</span>}</td>
              <td className="num">{r.exists ? fmt(r.datasets) : ""}</td>
              <td className={r.selected < r.datasets ? "num warn-text" : "num"}>{r.exists ? fmt(r.selected) : ""}</td>
              <td>{r.exists && <CampaignChips campaigns={r.campaigns} />}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row" colSpan={2}>
              Total
            </th>
            <td className="num strong">{fmt(total)}</td>
            <td />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

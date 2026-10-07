import Link from "next/link";
import type { AnalysisSummary } from "@/lib/api";
import { analysisHref, fmt } from "@/lib/format";

/** Sample lists down the side, campaigns across the top, dataset counts in the cells. */
export function CampaignMatrix({
  analysis,
  gitRef,
  selected,
}: {
  analysis: AnalysisSummary;
  gitRef?: string;
  selected?: { list?: string; campaign?: string };
}) {
  const campaigns = Object.keys(analysis.campaigns);
  return (
    <div className="table-scroll">
      <table className="matrix">
        <thead>
          <tr>
            <th scope="col">Sample list</th>
            <th scope="col" className="num">
              Datasets
            </th>
            {campaigns.map((c) => (
              <th key={c} scope="col" className={`num ${c === "unknown" ? "warn-text" : ""}`}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {analysis.sample_lists.map((s) => {
            const active = selected?.list === s.name;
            return (
              <tr key={s.name} className={active ? "row-active" : undefined}>
                <th scope="row">
                  <Link href={analysisHref(analysis.folder, gitRef, { list: s.name })}>{s.name}</Link>
                  {s.groups > 0 && <span className="muted small"> · {s.groups} groups</span>}
                </th>
                <td className="num">{s.datasets === 0 ? <span className="warn-text">empty</span> : fmt(s.datasets)}</td>
                {campaigns.map((c) => {
                  const n = s.campaigns[c];
                  const isSel = active && selected?.campaign === c;
                  return (
                    <td key={c} className={`num ${isSel ? "cell-active" : ""}`}>
                      {n ? (
                        <Link
                          href={analysisHref(analysis.folder, gitRef, { list: s.name, campaign: c })}
                          className={c === "unknown" ? "warn-text" : undefined}
                        >
                          {fmt(n)}
                        </Link>
                      ) : (
                        <span className="faint">·</span>
                      )}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row">Total</th>
            <td className="num">{fmt(analysis.sample_lists.reduce((n, s) => n + s.datasets, 0))}</td>
            {campaigns.map((c) => (
              <td key={c} className="num">
                {fmt(analysis.campaigns[c])}
              </td>
            ))}
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

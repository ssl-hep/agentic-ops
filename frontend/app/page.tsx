import Link from "next/link";
import type { ReactNode } from "react";
import { CampaignChips } from "@/components/CampaignChips";
import { ErrorPanel } from "@/components/ErrorPanel";
import { RefBar } from "@/components/RefBar";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type AnalysisList, type AnalysisSummary } from "@/lib/api";
import { analysisHref, fmt, param } from "@/lib/format";

export default async function AnalysesPage(props: PageProps<"/">) {
  const gitRef = param(await props.searchParams, "ref");

  let data: AnalysisList;
  try {
    data = await api.analyses(gitRef);
  } catch (error) {
    return <ErrorPanel error={error} />;
  }
  const refs = await api.refs().catch(() => null);
  const runnable = data.analyses.filter((a) => a.status === "runnable").length;

  return (
    <>
      <header className="page-head">
        <h1>Analyses</h1>
        <p className="muted">
          Folders under <span className="mono">data/</span> in EWKit. {runnable} of {data.analyses.length} can run as
          they are.
        </p>
      </header>
      <RefBar action="/" refs={refs} resolved={data.ref} image={data.image} />
      <div className="table-scroll">
        <table className="analyses">
          <thead>
            <tr>
              <th scope="col">Analysis</th>
              <th scope="col">Status</th>
              <th scope="col">Configs</th>
              <th scope="col">Sample lists</th>
              <th scope="col">Campaigns</th>
              <th scope="col">Code owners</th>
            </tr>
          </thead>
          <tbody>
            {data.analyses.map((a) => (
              <AnalysisRow key={a.folder} analysis={a} gitRef={gitRef} />
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function AnalysisRow({ analysis: a, gitRef }: { analysis: AnalysisSummary; gitRef?: string }) {
  return (
    <tr>
      <th scope="row">
        <Link href={analysisHref(a.folder, gitRef)} className="strong">
          {a.folder}
        </Link>
        {a.extends && <div className="small muted">extends {a.extends.split("/")[1] ?? a.extends}</div>}
      </th>
      <td>
        <StatusBadge status={a.status} />
        {a.status_reason && <div className="small muted reason">{a.status_reason}</div>}
        {a.warnings.length > 0 && (
          <div className="small warn-text">
            {a.warnings.length} warning{a.warnings.length > 1 ? "s" : ""}
          </div>
        )}
      </td>
      <td className="mono small">
        {a.configs.map((c) => (
          <div key={c.name}>{c.name}</div>
        ))}
      </td>
      <td className="small">
        {a.runs.map((run) => (
          <div key={run} className="nowrap">
            <span className="muted">{run}</span>{" "}
            {a.sample_lists
              .filter((s) => s.run === run)
              .map((s) => (
                <span key={s.name} className={s.datasets === 0 ? "warn-text" : undefined}>
                  {s.sample} {fmt(s.datasets)}
                </span>
              ))
              .reduce<ReactNode[]>((acc, el, i) => (i ? [...acc, " · ", el] : [el]), [])}
          </div>
        ))}
        {a.sample_lists.length === 0 && <span className="faint">none</span>}
      </td>
      <td>
        <CampaignChips campaigns={a.campaigns} />
      </td>
      <td className="small">{a.code_owners.join(" ")}</td>
    </tr>
  );
}

import Link from "next/link";
import { ErrorPanel } from "@/components/ErrorPanel";
import { RefBar } from "@/components/RefBar";
import { RunForm } from "@/components/RunForm";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type AnalysisDetail } from "@/lib/api";
import { analysisHref, href, param } from "@/lib/format";
import { submitRun } from "./actions";

export default async function ConfigureRunPage(props: PageProps<"/analyses/[folder]/run">) {
  const { folder: rawFolder } = await props.params;
  const folder = decodeURIComponent(rawFolder);
  const gitRef = param(await props.searchParams, "ref");

  let data: AnalysisDetail;
  try {
    data = await api.analysis(folder, gitRef);
  } catch (error) {
    return <ErrorPanel error={error} />;
  }
  const refs = await api.refs().catch(() => null);
  const a = data.analysis;

  return (
    <>
      <nav className="crumbs">
        <Link href={href("/", { ref: gitRef })}>Analyses</Link> /{" "}
        <Link href={analysisHref(a.folder, gitRef)}>{a.folder}</Link> / <span>Configure run</span>
      </nav>
      <header className="page-head">
        <h1>
          Configure run <StatusBadge status={a.status} />
        </h1>
        <p className="muted">
          Run <span className="mono">{a.folder}</span> at one EWKit commit. Change the ref here to pin a different one.
        </p>
      </header>
      <RefBar action={analysisHref(a.folder) + "/run"} refs={refs} resolved={data.ref} image={data.image} />

      {a.warnings.length > 0 && (
        <ul className="panel panel-warn warnings">
          {a.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}

      {/* Keyed by SHA so the form's defaults follow a ref change */}
      <RunForm key={data.ref.sha} analysis={a} resolved={data.ref} image={data.image} submitRun={submitRun} />
    </>
  );
}

import Link from "next/link";
import { CampaignMatrix } from "@/components/CampaignMatrix";
import { ErrorPanel } from "@/components/ErrorPanel";
import { RefBar } from "@/components/RefBar";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type AnalysisDetail, type ConfigInfo, type SampleListDetail } from "@/lib/api";
import { analysisHref, fmt, href, param } from "@/lib/format";

export default async function AnalysisPage(props: PageProps<"/analyses/[folder]">) {
  const { folder: rawFolder } = await props.params;
  const folder = decodeURIComponent(rawFolder);
  const searchParams = await props.searchParams;
  const gitRef = param(searchParams, "ref");
  const list = param(searchParams, "list");
  const campaign = param(searchParams, "campaign");
  const query = param(searchParams, "q");

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
        <Link href={href("/", { ref: gitRef })}>Analyses</Link> / <span>{a.folder}</span>
      </nav>
      <header className="page-head">
        <h1>
          {a.folder} <StatusBadge status={a.status} />
        </h1>
        {a.status_reason && <p>{a.status_reason}</p>}
        <p className="muted small">
          <a href={a.web_url} target="_blank" rel="noreferrer" className="mono">
            {a.path}/
          </a>
          {a.code_owners.length > 0 && <> · code owners {a.code_owners.join(" ")}</>}
          {a.ntuple_lists.length > 0 && <> · {a.ntuple_lists.length} n-tuple lists</>}
        </p>
      </header>
      <RefBar action={analysisHref(a.folder)} refs={refs} resolved={data.ref} image={data.image} />

      {a.warnings.length > 0 && (
        <ul className="panel panel-warn warnings">
          {a.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}

      <section>
        <h2>Configs</h2>
        <div className="cards">
          {a.configs.map((c) => (
            <ConfigCard key={c.name} config={c} folder={a.folder} gitRef={gitRef} />
          ))}
        </div>
      </section>

      <section>
        <h2>Datasets by campaign</h2>
        {a.sample_lists.length === 0 ? (
          <p className="muted">
            No sample lists in <span className="mono">DAODs/</span>.
          </p>
        ) : (
          <>
            <p className="muted small">
              Campaigns come from each dataset&rsquo;s r-tag, using the map in EWKit&rsquo;s{" "}
              <span className="mono">EWGridRun.py</span>. Select a list or a count to see its datasets.
            </p>
            <CampaignMatrix analysis={a} gitRef={gitRef} selected={{ list, campaign }} />
          </>
        )}
      </section>

      {list && (
        <DatasetSection folder={a.folder} list={list} gitRef={gitRef} campaign={campaign} query={query} />
      )}
    </>
  );
}

function ConfigCard({ config: c, folder, gitRef }: { config: ConfigInfo; folder: string; gitRef?: string }) {
  return (
    <article className="card">
      <h3 className="mono">
        <Link href={href(`/analyses/${encodeURIComponent(folder)}/configs/${encodeURIComponent(c.name)}`, { ref: gitRef })}>
          {c.name}
        </Link>
      </h3>
      {c.extend_chain.length > 0 && (
        <p className="small">
          Extends{" "}
          {c.extend_chain.map((step, i) => (
            <span key={step.path}>
              {i > 0 && " → "}
              {step.web_url ? (
                <a href={step.web_url} target="_blank" rel="noreferrer" className={step.exists ? "mono" : "mono bad-text"}>
                  {step.path}
                </a>
              ) : (
                <span className="mono bad-text">{step.path}</span>
              )}
              {!step.exists && <span className="bad-text"> (missing)</span>}
            </span>
          ))}
        </p>
      )}
      {c.parse_error && <p className="small bad-text">Does not parse: {c.parse_error}</p>}
      {c.output_tree && (
        <p className="small">
          Output tree <span className="mono">{c.output_tree}</span>
        </p>
      )}
      {c.blocks.length > 0 && (
        <p className="small muted">
          {c.extend_chain.length > 0 ? "Overrides" : "Blocks"}: {c.blocks.join(", ")}
        </p>
      )}
      {c.unavailable_modules.length > 0 && (
        <p className="small bad-text">Not in the standard image: {c.unavailable_modules.join(", ")}</p>
      )}
    </article>
  );
}

async function DatasetSection({
  folder,
  list,
  gitRef,
  campaign,
  query,
}: {
  folder: string;
  list: string;
  gitRef?: string;
  campaign?: string;
  query?: string;
}) {
  let detail: SampleListDetail;
  try {
    detail = await api.sampleList(folder, list, gitRef);
  } catch (error) {
    return <ErrorPanel error={error} />;
  }
  const needle = query?.toLowerCase();
  const groups = detail.groups
    .map((g) => ({
      ...g,
      datasets: g.datasets.filter(
        (d) => (!campaign || d.campaign === campaign) && (!needle || d.name.toLowerCase().includes(needle)),
      ),
    }))
    .filter((g) => g.datasets.length > 0);
  const shown = groups.reduce((n, g) => n + g.datasets.length, 0);

  return (
    <section id="datasets">
      <h2>
        <span className="mono">{list}.txt</span>
      </h2>
      <form method="get" action={`/analyses/${encodeURIComponent(folder)}#datasets`} className="filters">
        <input type="hidden" name="list" value={list} />
        {gitRef && <input type="hidden" name="ref" value={gitRef} />}
        <label>
          Campaign{" "}
          <select name="campaign" defaultValue={campaign ?? ""}>
            <option value="">all</option>
            {Object.entries(detail.summary.campaigns).map(([c, n]) => (
              <option key={c} value={c}>
                {c} ({fmt(n)})
              </option>
            ))}
          </select>
        </label>
        <label>
          Name contains{" "}
          <input name="q" defaultValue={query ?? ""} placeholder="DSID, generator, p-tag…" spellCheck={false} />
        </label>
        <button type="submit">Filter</button>
        <span className="muted small">
          {fmt(shown)} of {fmt(detail.summary.datasets)} datasets ·{" "}
          <a href={detail.summary.web_url} target="_blank" rel="noreferrer">
            view on GitLab
          </a>
        </span>
      </form>
      <div className="table-scroll">
        <table className="datasets">
          <thead>
            <tr>
              <th scope="col">DSID</th>
              <th scope="col">Campaign</th>
              <th scope="col">Dataset</th>
            </tr>
          </thead>
          {groups.map((g, i) => (
            <tbody key={`${g.heading}-${i}`}>
              {g.heading && (
                <tr className="group-row">
                  <th colSpan={3} scope="rowgroup">
                    {g.heading} <span className="muted">· {fmt(g.datasets.length)}</span>
                  </th>
                </tr>
              )}
              {g.datasets.map((d, j) => (
                <tr key={`${d.name}-${j}`}>
                  <td className="mono">{d.dsid ?? ""}</td>
                  <td className={d.problem ? "warn-text" : undefined}>{d.campaign}</td>
                  <td className="mono small dataset-name">
                    {d.name}
                    {d.problem && <div className="warn-text">{d.problem}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </div>
      {shown === 0 && <p className="muted">No datasets match.</p>}
    </section>
  );
}

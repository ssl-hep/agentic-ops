import Link from "next/link";
import { ErrorPanel } from "@/components/ErrorPanel";
import { api, type ConfigFile } from "@/lib/api";
import { analysisHref, href, param } from "@/lib/format";

export default async function ConfigPage(props: PageProps<"/analyses/[folder]/configs/[name]">) {
  const { folder: rawFolder, name: rawName } = await props.params;
  const folder = decodeURIComponent(rawFolder);
  const name = decodeURIComponent(rawName);
  const gitRef = param(await props.searchParams, "ref");

  let data: ConfigFile;
  try {
    data = await api.configFile(folder, name, gitRef);
  } catch (error) {
    return <ErrorPanel error={error} />;
  }

  return (
    <>
      <nav className="crumbs">
        <Link href={href("/", { ref: gitRef })}>Analyses</Link> /{" "}
        <Link href={analysisHref(folder, gitRef)}>{folder}</Link> / <span>{name}</span>
      </nav>
      <header className="page-head">
        <h1 className="mono">{data.config.path}</h1>
        <p className="muted small">
          At{" "}
          <a href={data.ref.web_url} target="_blank" rel="noreferrer" className="mono">
            {data.ref.short_sha}
          </a>{" "}
          ({data.ref.ref}) ·{" "}
          <a href={data.config.web_url} target="_blank" rel="noreferrer">
            view on GitLab
          </a>
          {data.config.extend_chain.length > 0 && (
            <>
              {" "}
              · extends{" "}
              {data.config.extend_chain.map((s) => (
                <span key={s.path} className="mono">
                  {s.path}{" "}
                </span>
              ))}
            </>
          )}
        </p>
      </header>
      <pre className="code">{data.content}</pre>
    </>
  );
}

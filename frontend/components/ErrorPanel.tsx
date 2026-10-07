import Link from "next/link";
import { ApiError, api, type ConnectionStatus } from "@/lib/api";

/** Explains a failed backend call; when GitLab is the problem, points at the token setup. */
export async function ErrorPanel({ error }: { error: unknown }) {
  const message = error instanceof Error ? error.message : String(error);
  const status = error instanceof ApiError && error.status !== 503 ? await api.status().catch(() => null) : null;
  const notFound = error instanceof ApiError && error.status === 404 && status?.reachable;

  return (
    <section className="panel panel-error" role="alert">
      <h2>{notFound ? "Not found" : "Cannot load EWKit"}</h2>
      <p>{message}</p>
      {status && !status.reachable && <SetupHint status={status} />}
      {notFound && (
        <p>
          <Link href="/">Back to the analyses on the default branch</Link>
        </p>
      )}
    </section>
  );
}

function SetupHint({ status }: { status: ConnectionStatus }) {
  return (
    <>
      <p>
        The backend reads <span className="mono">{status.project}</span> on{" "}
        <span className="mono">{status.gitlab_url}</span>
        {status.token_configured ? " with a token that does not work." : " without a token."}
        {status.detail && <> {status.detail}</>}
      </p>
      <p>
        Create a read-only GitLab token and install it as the <span className="mono">agentic-ops-gitlab</span>{" "}
        Secret, as described under &ldquo;GitLab token&rdquo; in the README.
      </p>
    </>
  );
}

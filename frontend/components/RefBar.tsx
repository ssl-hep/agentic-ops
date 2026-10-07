import type { ImageStatus, RefList, ResolvedRef } from "@/lib/api";
import { fmtDate } from "@/lib/format";

/**
 * Ref picker plus a summary of the resolved commit and its CI image. A plain GET form, so it
 * works without client-side JavaScript; the datalist suggests branches and tags, and any
 * commit SHA can be typed in.
 */
export function RefBar({
  action,
  refs,
  resolved,
  image,
}: {
  action: string;
  refs: RefList | null;
  resolved: ResolvedRef;
  image: ImageStatus;
}) {
  return (
    <section className="refbar">
      <form action={action} method="get" className="refbar-form">
        <label htmlFor="ref">EWKit ref</label>
        <input
          id="ref"
          name="ref"
          list="ref-options"
          defaultValue={resolved.ref}
          placeholder="branch, tag or commit"
          autoComplete="off"
          spellCheck={false}
        />
        <datalist id="ref-options">
          {refs?.branches.map((b) => (
            <option key={`b-${b.name}`} value={b.name}>
              {b.default ? "default branch" : "branch"}
            </option>
          ))}
          {refs?.tags.map((t) => (
            <option key={`t-${t.name}`} value={t.name}>
              {t.protected ? "protected tag" : "tag"}
            </option>
          ))}
        </datalist>
        <button type="submit">Browse</button>
      </form>
      <dl className="refbar-facts">
        <div>
          <dt>Commit</dt>
          <dd>
            <a href={resolved.web_url} target="_blank" rel="noreferrer" className="mono">
              {resolved.short_sha}
            </a>{" "}
            {resolved.title}
            <span className="muted">
              {" "}
              · {resolved.author_name} · {fmtDate(resolved.committed_date)}
            </span>
          </dd>
        </div>
        <div>
          <dt>CI image</dt>
          <dd>
            <ImageLine image={image} />
          </dd>
        </div>
      </dl>
    </section>
  );
}

function ImageLine({ image }: { image: ImageStatus }) {
  if (image.available) {
    return (
      <>
        <span className="dot dot-ok" aria-hidden /> <span className="mono">{image.location}</span>
        <span className="muted">
          {" "}
          · {image.digest?.slice(0, 19)}… · pushed {fmtDate(image.created_at)}
        </span>
      </>
    );
  }
  const tone = image.available === false ? "dot-bad" : "dot-unknown";
  return (
    <>
      <span className={`dot ${tone}`} aria-hidden />{" "}
      {image.tag && <span className="mono">:{image.tag} </span>}
      <span className="muted">{image.detail}</span>
    </>
  );
}

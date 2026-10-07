import Link from "next/link";
import { decideCard } from "@/app/setups/actions";
import type { IssueCard as Card, RunSpec } from "@/lib/api";
import { href } from "@/lib/format";
import { EvidenceList, SeverityBadge } from "./FindingList";
import { SubmitButton } from "./SubmitButton";

const show = (value: unknown) =>
  Array.isArray(value) ? (value.length ? value.join(", ") : "none") : value === null ? "auto" : String(value);

/** One reviewer issue, laid out like the proposal's mock-up, with Accept / Edit / Dismiss. */
export function IssueCard({
  card,
  spec,
  setupId,
  canAct,
}: {
  card: Card;
  spec: RunSpec | null;
  setupId: string;
  canAct: boolean;
}) {
  const patch = card.patch ? Object.entries(card.patch) : [];
  const acceptable = patch.length > 0 && !card.patch_error;
  return (
    <article className={`card issue issue-${card.severity}`}>
      <header className="issue-head">
        <SeverityBadge severity={card.severity} /> <span className="small muted">
          {card.source === "validator" ? "Validator (the reviewer did not cover this)" : "Reviewer agent"}
        </span>
        {card.finding_codes.map((code) => (
          <span key={code} className="chip">
            {code}
          </span>
        ))}
      </header>
      <h3>{card.title}</h3>
      <p>{card.explanation}</p>

      {card.evidence.length > 0 && (
        <div className="issue-row">
          <div className="issue-label">Evidence</div>
          <EvidenceList evidence={card.evidence} />
        </div>
      )}
      {patch.length > 0 && (
        <div className="issue-row">
          <div className="issue-label">Proposal</div>
          <ul className="patch">
            {patch.map(([field, value]) => (
              <li key={field}>
                <span className="mono">{field}</span>:{" "}
                <span className="mono muted">{show(spec?.[field as keyof RunSpec])}</span> →{" "}
                <span className="mono strong">{show(value)}</span>
              </li>
            ))}
          </ul>
          {card.patch_error && <p className="small bad-text">This patch does not fit the RunSpec: {card.patch_error}</p>}
        </div>
      )}
      {card.effect && (card.effect.clears.length > 0 || card.effect.introduces.length > 0) && (
        <div className="issue-row">
          <div className="issue-label">Effect</div>
          <div className="small">
            {card.effect.clears.length > 0 && <div>Clears {card.effect.clears.join(", ")}</div>}
            {card.effect.introduces.length > 0 && (
              <div className="warn-text">Introduces {card.effect.introduces.join(", ")}</div>
            )}
          </div>
        </div>
      )}
      {card.if_declined && (
        <div className="issue-row">
          <div className="issue-label">If declined</div>
          <div className="small">{card.if_declined}</div>
        </div>
      )}
      {card.owners.length > 0 && (
        <div className="issue-row">
          <div className="issue-label">Code owners</div>
          <div className="small">{card.owners.join(" ")}</div>
        </div>
      )}

      {canAct && (
        <form action={decideCard} className="issue-actions">
          <input type="hidden" name="id" value={setupId} />
          {acceptable && (
            <SubmitButton name="accept" value={card.id} pendingText="Applying and re-checking…">
              Accept patch
            </SubmitButton>
          )}
          {acceptable && (
            <Link href={href(`/setups/${encodeURIComponent(setupId)}`, { prefill: card.id }) + "#spec"} className="button-link">
              Edit
            </Link>
          )}
          <SubmitButton name="dismiss" value={card.id} className="secondary" pendingText="Dismissing…">
            {acceptable ? "Dismiss" : "Got it"}
          </SubmitButton>
        </form>
      )}
    </article>
  );
}

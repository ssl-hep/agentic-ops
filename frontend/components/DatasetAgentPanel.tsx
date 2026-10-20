"use client";

import { useEffect, useRef, useState, useTransition, type KeyboardEvent } from "react";
import type { ActionResult } from "@/app/analyses/[folder]/run/selection-actions";
import type { Selection, SelectionReply, SelectionSummary } from "@/lib/api";
import { analysisHref, fmt } from "@/lib/format";

export interface ActiveSelection {
  id: string;
  count: number;
}

interface Turn {
  role: "user" | "assistant";
  content: string;
  tools?: string[];
}

type CreateSelection = (folder: string, ref: string | null) => Promise<ActionResult<Selection>>;
type SendSelectionMessage = (folder: string, id: string, content: string) => Promise<ActionResult<SelectionReply>>;
type GetSelection = (folder: string, id: string) => Promise<ActionResult<Selection>>;

/** Remembers the conversation for this analysis and commit across reloads, in this tab only */
const storageKey = (folder: string, sha: string) => `selection:${folder}:${sha}`;

function remembered(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function remember(key: string, id: string | null) {
  try {
    if (id) sessionStorage.setItem(key, id);
    else sessionStorage.removeItem(key);
  } catch {
    // Storage blocked: the conversation just does not survive a reload
  }
}

/**
 * Chat with the dataset agent to narrow the sample lists to an exact set of datasets, by run, sample
 * name and campaign, checking Rucio replication on the way. The backend keeps the selection as a
 * scratch sample-list file that the run can use in place of the checkboxes.
 */
export function DatasetAgentPanel({
  folder,
  sha,
  active,
  onActiveChange,
  createSelection,
  sendSelectionMessage,
  getSelection,
}: {
  folder: string;
  /** Commit the run is pinned to; the selection reads the sample lists at the same one */
  sha: string;
  active: ActiveSelection | null;
  onActiveChange: (selection: ActiveSelection | null) => void;
  createSelection: CreateSelection;
  sendSelectionMessage: SendSelectionMessage;
  getSelection: GetSelection;
}) {
  const key = storageKey(folder, sha);
  const [id, setId] = useState<string | null>(null);
  const [summary, setSummary] = useState<SelectionSummary | null>(null);
  const [available, setAvailable] = useState<number | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const transcript = useRef<HTMLOListElement>(null);

  // Pick up a conversation from earlier in this tab
  useEffect(() => {
    const saved = remembered(key);
    if (!saved) return;
    startTransition(async () => {
      const result = await getSelection(folder, saved);
      if (!result.ok) {
        remember(key, null);
        return;
      }
      load(result.data);
    });
  }, [folder, key, getSelection]);

  useEffect(() => {
    transcript.current?.scrollTo({ top: transcript.current.scrollHeight });
  }, [turns, pending]);

  function load(selection: Selection) {
    setId(selection.id);
    setSummary(selection.selection);
    setAvailable(selection.available?.count ?? null);
    setTurns(selection.messages);
  }

  function apply(next: SelectionSummary, selectionId: string) {
    setSummary(next);
    // Keep the run form's count in step, and stop using a selection the agent emptied
    if (active?.id === selectionId) onActiveChange(next.count > 0 ? { id: selectionId, count: next.count } : null);
  }

  function start() {
    setError(null);
    startTransition(async () => {
      const result = await createSelection(folder, sha);
      if (!result.ok) {
        setError(result.error);
        return;
      }
      load(result.data);
      remember(key, result.data.id);
    });
  }

  function startOver() {
    remember(key, null);
    if (active) onActiveChange(null);
    setId(null);
    setSummary(null);
    setAvailable(null);
    setTurns([]);
    setDraft("");
    setError(null);
  }

  function send(event: { preventDefault(): void }) {
    event.preventDefault();
    const content = draft.trim();
    if (!id || !content || pending) return;
    setError(null);
    setDraft("");
    setTurns((t) => [...t, { role: "user", content }]);
    startTransition(async () => {
      const result = await sendSelectionMessage(folder, id, content);
      if (!result.ok) {
        setError(result.error);
        // Give the message back so it can be sent again
        setTurns((t) => t.slice(0, -1));
        setDraft(content);
        return;
      }
      const tools = result.data.tool_calls.map((c) => c.name);
      setTurns((t) => [...t, { role: "assistant", content: result.data.reply, tools }]);
      apply(result.data.selection, id);
    });
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) send(event);
  }

  const count = summary?.count ?? 0;
  const download = id ? `${analysisHref(folder)}/run/selections/${encodeURIComponent(id)}` : null;

  return (
    <section className="agent-panel" aria-label="Dataset agent">
      <header className="agent-head">
        <h2>Narrow the datasets</h2>
        {id && (
          <button type="button" className="button-quiet small" onClick={startOver} disabled={pending}>
            Start over
          </button>
        )}
      </header>

      {!id ? (
        <div className="agent-intro">
          <p className="muted small">
            Chat with the dataset agent to pick an exact subset of this analysis&rsquo;s sample lists: filter by run,
            sample name and campaign, and check which datasets Rucio has replicas of. The run can then use the
            agent&rsquo;s list instead of whole sample lists.
          </p>
          <button type="button" onClick={start} disabled={pending}>
            {pending ? "Starting…" : "Start"}
          </button>
        </div>
      ) : (
        <>
          <ol className="agent-transcript" ref={transcript} aria-live="polite">
            {turns.length === 0 && !pending && (
              <li className="agent-hint muted small">
                For example: &ldquo;Only Run3 Zee and Zmumu in mc23a and mc23d, and drop anything without a disk
                replica.&rdquo;
              </li>
            )}
            {turns.map((t, i) => (
              <li key={i} className={`agent-msg agent-msg-${t.role}`}>
                <span className="agent-role">{t.role === "user" ? "You" : "Agent"}</span>
                <div className="agent-text">{t.content}</div>
                {t.tools && t.tools.length > 0 && (
                  <div className="chips agent-tools" title="Tools the agent called for this answer">
                    {t.tools.map((name, j) => (
                      <span key={j} className="chip">
                        {name}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            ))}
            {pending && (
              <li className="agent-msg agent-msg-assistant agent-pending" role="status">
                <span className="agent-role">Agent</span>
                <div className="agent-text muted">Working… checking Rucio can take up to two minutes.</div>
              </li>
            )}
          </ol>

          <form className="agent-input" onSubmit={send}>
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKeyDown}
              rows={2}
              maxLength={4000}
              placeholder="Describe the datasets you want…"
              aria-label="Message to the dataset agent"
              disabled={pending}
            />
            <button type="submit" disabled={pending || !draft.trim()}>
              {pending ? "Waiting…" : "Send"}
            </button>
          </form>
        </>
      )}

      {error && (
        <p className="panel panel-error small" role="alert">
          {error}
        </p>
      )}

      {id && summary && (
        <div className="agent-summary">
          <p>
            <span className="strong">{fmt(count)}</span>
            {available !== null && <> of {fmt(available)}</>} dataset{(available ?? count) === 1 ? "" : "s"} selected
          </p>
          <SummaryChips label="Run" counts={summary.by_run} />
          <SummaryChips label="Sample" counts={summary.by_sample} />
          <SummaryChips label="Campaign" counts={summary.by_campaign} />
          <div className="agent-actions">
            <label className={count === 0 ? "faint" : undefined}>
              <input
                type="checkbox"
                checked={active?.id === id}
                disabled={count === 0}
                onChange={(e) => onActiveChange(e.target.checked ? { id, count } : null)}
              />{" "}
              Use this selection for the run
            </label>
            {download && count > 0 && (
              <a href={download} download className="small">
                Download datasets.txt
              </a>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function SummaryChips({ label, counts }: { label: string; counts: Record<string, number> }) {
  const entries = Object.entries(counts);
  if (entries.length === 0) return null;
  return (
    <div className="agent-chips">
      <span className="field-label small">{label}</span>
      <div className="chips">
        {entries.map(([name, n]) => (
          <span key={name} className="chip" title={`${fmt(n)} dataset${n === 1 ? "" : "s"}`}>
            {name} <span className="muted">{fmt(n)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

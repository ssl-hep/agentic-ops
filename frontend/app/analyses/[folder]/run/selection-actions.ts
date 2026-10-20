"use server";

import { api, type Selection, type SelectionReply } from "@/lib/api";

/** Errors come back as values, so a missing agent (503) shows in the panel instead of an error page. */
export type ActionResult<T> = { ok: true; data: T } | { ok: false; error: string };

async function attempt<T>(call: () => Promise<T>): Promise<ActionResult<T>> {
  try {
    return { ok: true, data: await call() };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

export async function createSelection(folder: string, ref: string | null): Promise<ActionResult<Selection>> {
  return attempt(() => api.createSelection(folder, ref));
}

export async function sendSelectionMessage(
  folder: string,
  id: string,
  content: string,
): Promise<ActionResult<SelectionReply>> {
  if (!content.trim()) return { ok: false, error: "Type a message first." };
  return attempt(() => api.sendSelectionMessage(folder, id, content.trim()));
}

export async function getSelection(folder: string, id: string): Promise<ActionResult<Selection>> {
  return attempt(() => api.selection(folder, id));
}

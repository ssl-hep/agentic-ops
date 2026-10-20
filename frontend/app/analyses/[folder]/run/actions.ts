"use server";

import { parseRunForm, type RunFormState } from "@/lib/run";

/**
 * Checks the run configuration and hands it back. A no-op for now: nothing is sent to the
 * backend, so no job is created. Submission will replace the final return.
 */
export async function submitRun(_prev: RunFormState, form: FormData): Promise<RunFormState> {
  const { request, errors } = parseRunForm(form);
  if (errors.length > 0) return { status: "invalid", errors };
  return { status: "noop", request };
}

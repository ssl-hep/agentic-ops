"use server";

// Server actions for run setup. They call the backend from the server, so the browser never talks
// to it, then redirect back to the setup page. Errors come back as ?error= on that page.
import { redirect } from "next/navigation";
import { api, type RunSpec, type SetupAction } from "@/lib/api";
import { href } from "@/lib/format";

const text = (form: FormData, key: string) => {
  const value = form.get(key);
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
};

const lines = (form: FormData, key: string) =>
  (text(form, key) ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

const message = (error: unknown) => (error instanceof Error ? error.message : String(error));

export async function createSetup(form: FormData) {
  const request = text(form, "request");
  const analysis = text(form, "analysis");
  const ref = text(form, "ref");
  let id: string;
  try {
    id = (await api.createSetup({ request, analysis, ref })).id;
  } catch (error) {
    redirect(href("/setups/new", { error: message(error), request, analysis, ref }));
  }
  redirect(`/setups/${encodeURIComponent(id)}`);
}

async function act(id: string, action: SetupAction) {
  const page = `/setups/${encodeURIComponent(id)}`;
  try {
    await api.setupAction(id, action);
  } catch (error) {
    redirect(href(page, { error: message(error) }));
  }
  redirect(page);
}

export async function editSpec(form: FormData) {
  const files = text(form, "files_per_job");
  const spec: RunSpec = {
    analysis: text(form, "analysis") ?? "",
    ref: text(form, "ref") ?? "",
    config: text(form, "config") ?? "config.yaml",
    runs: form.getAll("runs").map(String),
    sample_lists: form.getAll("sample_lists").map(String),
    systematics: (text(form, "systematics") ?? "config-default") as RunSpec["systematics"],
    no_filter: form.get("no_filter") === "on",
    no_thinning: form.get("no_thinning") === "on",
    include: lines(form, "include"),
    skip: lines(form, "skip"),
    purpose: (text(form, "purpose") ?? "test") as RunSpec["purpose"],
    memory_mb: Number(text(form, "memory_mb") ?? 2000),
    files_per_job: files ? Number(files) : null,
  };
  await act(text(form, "id") ?? "", { action: "edit", spec });
}

/** One card's Accept or Dismiss button: the clicked button's name and value say which. */
export async function decideCard(form: FormData) {
  const accept = text(form, "accept");
  const dismiss = text(form, "dismiss");
  await act(text(form, "id") ?? "", {
    action: "apply",
    accept: accept ? [accept] : [],
    dismiss: dismiss ? [dismiss] : [],
  });
}

export async function redraft(form: FormData) {
  await act(text(form, "id") ?? "", { action: "redraft", request: text(form, "request") ?? "" });
}

export async function finish(form: FormData) {
  await act(text(form, "id") ?? "", { action: "finish" });
}

"use server";

// Server actions for run requests. They call the backend from the server, so the browser never talks
// to it, then redirect back to the request page. Errors come back as ?error= on that page.
import { redirect } from "next/navigation";
import { api, type RequestAction } from "@/lib/api";
import { href } from "@/lib/format";

const text = (form: FormData, key: string) => {
  const value = form.get(key);
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
};

const message = (error: unknown) => (error instanceof Error ? error.message : String(error));

export async function createRequest(form: FormData) {
  const request = text(form, "request");
  const analysis = text(form, "analysis");
  const ref = text(form, "ref");
  let id: string;
  try {
    id = (await api.createRequest({ request, analysis, ref })).id;
  } catch (error) {
    redirect(href("/requests/new", { error: message(error), request, analysis, ref }));
  }
  redirect(`/requests/${encodeURIComponent(id)}`);
}

async function act(id: string, action: RequestAction) {
  const page = `/requests/${encodeURIComponent(id)}`;
  try {
    await api.requestAction(id, action);
  } catch (error) {
    redirect(href(page, { error: message(error) }));
  }
  redirect(page);
}

export async function editDatasets(form: FormData) {
  const datasets = (text(form, "datasets") ?? "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  await act(text(form, "id") ?? "", { action: "edit", datasets });
}

export async function redraftRequest(form: FormData) {
  await act(text(form, "id") ?? "", { action: "redraft", request: text(form, "request") ?? "" });
}

export async function finishRequest(form: FormData) {
  await act(text(form, "id") ?? "", { action: "finish" });
}

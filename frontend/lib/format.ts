export type SearchParams = Record<string, string | string[] | undefined>;

export function param(params: SearchParams, key: string): string | undefined {
  const value = params[key];
  const first = Array.isArray(value) ? value[0] : value;
  return first?.trim() || undefined;
}

/** Build an in-app link, dropping empty query values. */
export function href(path: string, query: Record<string, string | undefined> = {}): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value) search.set(key, value);
  }
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

export function analysisHref(folder: string, ref?: string, query: Record<string, string | undefined> = {}) {
  return href(`/analyses/${encodeURIComponent(folder)}`, { ref, ...query });
}

const numberFormat = new Intl.NumberFormat("en-US");
export const fmt = (n: number) => numberFormat.format(n);

export function fmtDate(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short", timeZone: "UTC" }) + " UTC";
}

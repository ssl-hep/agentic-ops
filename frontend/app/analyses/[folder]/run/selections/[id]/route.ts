import { selectionDatasetsUrl } from "@/lib/api";

// Proxies the agent's sample-list file so the browser can download it without reaching the backend
export async function GET(_request: Request, ctx: RouteContext<"/analyses/[folder]/run/selections/[id]">) {
  const { folder: rawFolder, id } = await ctx.params;
  const folder = decodeURIComponent(rawFolder);
  let response: Response;
  try {
    response = await fetch(selectionDatasetsUrl(folder, id), { cache: "no-store" });
  } catch {
    return new Response("Cannot reach the backend.\n", { status: 503, headers: { "Content-Type": "text/plain" } });
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail ?? `Backend returned ${response.status}`;
    return new Response(`${detail}\n`, { status: response.status, headers: { "Content-Type": "text/plain" } });
  }
  const filename = `${folder}.selection-${id}.txt`.replace(/[^A-Za-z0-9._-]/g, "_");
  return new Response(response.body, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Content-Disposition": `attachment; filename="${filename}"`,
      "Cache-Control": "no-store",
    },
  });
}

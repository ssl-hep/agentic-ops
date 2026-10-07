// Liveness/readiness endpoint that does not depend on the backend or GitLab
export function GET() {
  return Response.json({ status: "ok" });
}

export const dynamic = "force-dynamic";

export function GET() {
  const projectToken = process.env.NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN || "";
  const payload = {
    projectToken,
    apiHost: projectToken ? "/ingest" : "",
    uiHost: process.env.NEXT_PUBLIC_POSTHOG_UI_HOST || "https://us.posthog.com",
    enabled: Boolean(projectToken),
  };

  return Response.json(payload, {
    headers: {
      "Cache-Control": "public, max-age=300, stale-while-revalidate=3600",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

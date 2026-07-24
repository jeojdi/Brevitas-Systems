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
      "Cache-Control": "private, no-store, max-age=0, must-revalidate",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

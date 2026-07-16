import { PostHog } from "posthog-node";

let posthogClient: PostHog | null = null;

function getPostHogClient(): PostHog | null {
  const projectToken = process.env.NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN;
  if (!projectToken) return null;

  if (!posthogClient) {
    posthogClient = new PostHog(
      projectToken,
      {
        host: process.env.POSTHOG_HOST || "https://us.i.posthog.com",
        flushAt: 1,
        flushInterval: 0,
      }
    );
  }
  return posthogClient;
}

interface ServerAnalyticsEvent {
  distinctId: string;
  event: string;
  properties?: Record<string, unknown>;
}

export async function captureServerEvent(event: ServerAnalyticsEvent): Promise<void> {
  const client = getPostHogClient();
  if (!client) return;

  try {
    client.capture(event);
    await client.flush();
  } catch {
    // Analytics must never break a customer-facing request.
  }
}

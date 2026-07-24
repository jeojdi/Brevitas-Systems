import { createHash } from "node:crypto";
import { PostHog } from "posthog-node";

let posthogClient: PostHog | null = null;

const SAFE_EVENT = /^[a-z][a-z0-9_]{0,63}$/;
const SAFE_PROPERTIES = new Set([
  "event_type",
  "has_company",
  "has_orchestrator",
  "payment_outcome",
  "requested_design_partnership",
  "session_reused",
  "source",
  "subscription_status",
]);
const INTERNAL_PROPERTIES = new Set([
  "$geoip_disable",
  "$lib",
  "$lib_version",
  "$process_person_profile",
  "distinct_id",
  "token",
]);
const SAFE_STRING_VALUES: Record<string, Set<string>> = {
  event_type: new Set([
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.deleted",
    "customer.subscription.updated",
    "invoice.paid",
    "invoice.payment_failed",
  ]),
  payment_outcome: new Set(["failed", "paid"]),
  source: new Set(["stripe_webhook", "website_waitlist"]),
  subscription_status: new Set([
    "active", "canceled", "incomplete", "incomplete_expired", "past_due", "paused", "trialing", "unpaid",
  ]),
};
const SENSITIVE_VALUE = /(?:\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|\b(?:bearer|basic)\s+\S+|\b(?:sk|rk|phx|phs|whsec|xox[baprs]|gh[opusr])_[A-Za-z0-9_-]{6,})/i;

function pseudonymousDistinctId(value: string): string {
  // PostHog does not need an email, name, or raw account identifier. The input is
  // bounded before hashing so a caller cannot turn analytics into an allocation sink.
  const bounded = String(value || "anonymous").slice(0, 256);
  return `bvt_${createHash("sha256").update(bounded).digest("hex")}`;
}

function safeScalar(key: string, value: unknown): string | number | boolean | null | undefined {
  if (typeof value === "boolean" || typeof value === "number") return value;
  if (typeof value !== "string") return undefined;
  const bounded = value.slice(0, 80);
  if (SENSITIVE_VALUE.test(bounded)) return undefined;
  const allowed = SAFE_STRING_VALUES[key];
  return !allowed || allowed.has(bounded) ? bounded : undefined;
}

function sanitizeProperties(properties: Record<string, unknown> | undefined): Record<string, unknown> {
  const safe: Record<string, unknown> = { $geoip_disable: true, $process_person_profile: false };
  for (const [key, value] of Object.entries(properties || {})) {
    if (!SAFE_PROPERTIES.has(key)) continue;
    const scalar = safeScalar(key, value);
    if (scalar !== undefined) safe[key] = scalar;
  }
  return safe;
}

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
        maxQueueSize: 100,
        privacyMode: true,
        enableExceptionAutocapture: true,
        before_send: event => {
          // Autocapture is retained for SDK compatibility but exception events are
          // dropped because free-form exception messages can contain customer data.
          if (!event || event.event === "$exception") return null;
          const properties: Record<string, unknown> = {};
          for (const [key, value] of Object.entries(event.properties || {})) {
            if (!INTERNAL_PROPERTIES.has(key) && !SAFE_PROPERTIES.has(key)) continue;
            const scalar = safeScalar(key, value);
            if (scalar !== undefined) properties[key] = scalar;
          }
          return { ...event, properties };
        },
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
  if (!client || !SAFE_EVENT.test(event.event)) return;

  try {
    client.capture({
      distinctId: pseudonymousDistinctId(event.distinctId),
      event: event.event,
      properties: sanitizeProperties(event.properties),
    });
    await client.flush();
  } catch {
    // Analytics must never break a customer-facing request.
  }
}

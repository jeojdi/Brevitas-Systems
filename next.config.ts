import type { NextConfig } from "next";

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
];

const dashboardCsp = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' data: https://fonts.gstatic.com",
  "img-src 'self' data: blob:",
  "connect-src 'self' https://*.supabase.co wss://*.supabase.co",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "upgrade-insecure-requests",
].join("; ");

// The rest of the origin had NO Content-Security-Policy at all: `/`, `/product`,
// `/pricing`, `/docs`, `/blog/*`, `/orchestration`, `/benchmarks`, `/welcome` and
// `/email-confirmed` shipped with an unrestricted script-src and connect-src.
// CSP is origin-scoped in effect, not path-scoped: the SPA's Supabase session
// lives in localStorage on this same origin, so script running on any of those
// paths reads it and the dashboard's own policy above never applies.
//
// This is the marketing pages' real resource contract, and it is deliberately
// looser than dashboardCsp — those pages compile JSX in the browser, so they need
// their CDN hosts, inline <script type="text/babel"> and eval. `connect-src` is
// the directive that matters for the session-theft path: it stays 'self' plus
// Supabase, so a foothold cannot POST the token to an arbitrary host.
//
// ROLLOUT: shipped Report-Only first, on purpose. Enforcing it in the same change
// as writing it would risk taking the public site down for a directive nobody
// verified in a browser (posthog-js's replay worker is the known unknown). Step
// three is to promote the key below to "Content-Security-Policy" once one release
// has gone out with no unexpected violations reported by the browser console —
// prioritising /welcome and /email-confirmed, which parse location.hash/search
// and hand GoTrue's access token to the SPA.
const marketingCsp = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://cdn.jsdelivr.net",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' data: https://fonts.gstatic.com",
  "img-src 'self' data: blob:",
  "media-src 'self'",
  "worker-src 'self' blob:",
  "connect-src 'self' https://*.supabase.co wss://*.supabase.co",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "upgrade-insecure-requests",
].join("; ");

const dashboardHeaders = [
  { key: "Content-Security-Policy", value: dashboardCsp },
  { key: "X-Robots-Tag", value: "noindex, nofollow" },
];
const noIndexHeaders = [{ key: "X-Robots-Tag", value: "noindex, nofollow" }];
const posthogHost = (process.env.POSTHOG_HOST || "https://us.i.posthog.com").replace(/\/$/, "");
const posthogAssetsHost = (process.env.POSTHOG_ASSETS_HOST || "https://us-assets.i.posthog.com").replace(/\/$/, "");
function resolveBackendApiHost(): string {
  // Production surfaces on Vercel (VERCEL_ENV set) or any production build must
  // have BREVITAS_API_URL explicitly set to an https: origin. Otherwise the value
  // gets frozen into routes-manifest.json at build time and a missing env var
  // silently bakes localhost into the deployed rewrite.
  const isProduction = process.env.NODE_ENV === "production" || Boolean(process.env.VERCEL_ENV);
  const configured = process.env.BREVITAS_API_URL?.trim();

  if (isProduction) {
    if (!configured) {
      throw new Error(
        "BREVITAS_API_URL must be set for production builds (VERCEL_ENV set or NODE_ENV=production).",
      );
    }
    let protocol: string;
    try {
      protocol = new URL(configured).protocol;
    } catch {
      throw new Error(`BREVITAS_API_URL is not a valid URL: ${configured}`);
    }
    if (protocol !== "https:") {
      throw new Error(`BREVITAS_API_URL must use https: in production, got ${protocol}`);
    }
    return configured.replace(/\/$/, "");
  }

  // Local dev only (NODE_ENV !== 'production' and no VERCEL_ENV).
  return (configured || "http://localhost:8000").replace(/\/$/, "");
}

const backendApiHost = resolveBackendApiHost();

const nextConfig: NextConfig = {
  // Next injects an internal, unconditional `/:path+/` -> `/:path+` 308 redirect at the
  // very front of the redirect table (load-custom-routes.ts unshifts it with
  // `priority: true`). Redirects are evaluated before every rewrite, so that internal
  // rule strips the trailing slash from PostHog's ingest endpoints — `/ingest/i/v0/e/`,
  // `/ingest/flags/?v=2`, `/ingest/s/` — before the `/ingest/:path*` proxy rewrite can
  // ever run. No rewrite or redirect entry can pre-empt it; the only lever is this flag.
  //
  // Disabling it is global, so the equivalent redirect is re-added explicitly below for
  // every path except `/ingest/*`, preserving the site's existing trailing-slash SEO
  // behaviour exactly. This site has no App Router pages (only /api route handlers), so
  // the flag's other effect — `process.env.__NEXT_MANUAL_TRAILING_SLASH`, which relaxes
  // client-side `next/link` URL normalization — is inert here.
  skipTrailingSlashRedirect: true,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          ...securityHeaders,
          // Different header KEY from the dashboard's enforced policy, so the two
          // coexist on /dashboard/* rather than one overriding the other
          // (node_modules/next/dist/docs/01-app/03-api-reference/05-config/01-next-config-js/headers.md:
          // same key, last wins).
          { key: "Content-Security-Policy-Report-Only", value: marketingCsp },
        ],
      },
      ...["/dashboard/:path*", "/login", "/login/personal", "/login/enterprise", "/signup", "/waitlist", "/invite"]
        .map((source) => ({ source, headers: dashboardHeaders })),
      {
        // Everything Vite emits under /dashboard/assets carries a content hash in
        // its filename, so it can be cached forever — a new build gets new URLs.
        // Without this, these files inherit the public-folder default
        // (max-age=0, must-revalidate), which forces every page load — and every
        // edge POP — back through a cross-ocean revalidation with the US origin;
        // measured 300–530ms TTFB from Frankfurt/Singapore/Tokyo on 2026-08-06.
        // Deliberately scoped to the hashed directory only: /dashboard/index.html
        // and /dashboard/boot-dark.js keep stable names and MUST stay
        // revalidating or deploys would not reach returning browsers.
        source: "/dashboard/assets/:path*",
        headers: [
          { key: "Cache-Control", value: "public, max-age=31536000, immutable" },
        ],
      },
      {
        // Self-hosted font binaries (mirrored Google Fonts). Only the .woff2
        // files are immutable — /fonts/fonts.css keeps a stable name and is the
        // one file that must revalidate, or a regenerated font set could never
        // reach browsers that cached the old stylesheet for a year.
        source: "/fonts/:file(.+\\.woff2)",
        headers: [
          { key: "Cache-Control", value: "public, max-age=31536000, immutable" },
        ],
      },
      ...["/email-confirmed", "/welcome"]
        .map((source) => ({ source, headers: noIndexHeaders })),
    ];
  },
  async rewrites() {
    return {
      // beforeFiles runs before Next.js checks the filesystem or App Router —
      // required for rewrites that serve .html files from /public to actually fire.
      beforeFiles: [
        { source: '/', destination: '/index.html' },
        { source: '/product', destination: '/product.html' },
        { source: '/benchmarks', destination: '/benchmarks.html' },
        { source: '/docs', destination: '/docs.html' },
        { source: '/blog', destination: '/blog.html' },
        { source: '/pricing', destination: '/pricing.html' },
        { source: '/privacy', destination: '/privacy.html' },
        { source: '/terms', destination: '/terms.html' },
        { source: '/waitlist', destination: '/dashboard/index.html' },
        // The three /login* aliases are also the landing sites for a confirmed signup:
        // public/email-confirmed.html forwards GoTrue's `#access_token=…` fragment onto
        // one of them so the SPA's auth-js client consumes it (implicit flow,
        // detectSessionInUrl) and persists the session. Repointing any of these away
        // from the SPA silently drops confirmed users back to a logged-out state.
        { source: '/login', destination: '/dashboard/index.html' },
        { source: '/login/personal', destination: '/dashboard/index.html' },
        { source: '/login/enterprise', destination: '/dashboard/index.html' },
        { source: '/signup', destination: '/dashboard/index.html' },
        { source: '/invite', destination: '/dashboard/index.html' },
        // Stays on the static page rather than the SPA: it is the only surface that
        // renders GoTrue's `error_description` and the `?audience=` split, neither of
        // which the SPA models. It hands the session fragment to the SPA instead.
        { source: '/email-confirmed', destination: '/email-confirmed.html' },
        { source: '/welcome', destination: '/welcome.html' },
        { source: '/blog/fable-5', destination: '/fable-5.html' },
        { source: '/blog/60-percent-waste', destination: '/60-percent-waste.html' },
        { source: '/blog/why-caches-dont-help', destination: '/why-caches-dont-help.html' },
        { source: '/blog/compression-without-losing-quality', destination: '/compression-without-losing-quality.html' },
        { source: '/blog/git-for-agent-context', destination: '/git-for-agent-context.html' },
        { source: '/blog/harness', destination: '/harness.html' },
        { source: '/blog/design-partner-program', destination: '/design-partner-program.html' },
        { source: '/dashboard', destination: '/dashboard/index.html' },
        { source: '/orchestration', destination: '/orchestration.html' },
      ],
      afterFiles: [
        // Keep the specific SDK asset paths before the catch-all ingestion proxy.
        { source: '/ingest/static/:path*', destination: `${posthogAssetsHost}/static/:path*` },
        { source: '/ingest/array/:path*', destination: `${posthogAssetsHost}/array/:path*` },
        { source: '/ingest/:path*', destination: `${posthogHost}/:path*` },
        {
          source: '/v1/:path*',
          destination: `${backendApiHost}/v1/:path*`,
        },
      ],
    };
  },
  async redirects() {
    return [
      // Replacement for the internal trailing-slash redirect disabled by
      // `skipTrailingSlashRedirect` above. Mirrors Next's own `/:path+/` -> `/:path+`
      // rule (relative destination, so the host is preserved and the www redirect below
      // still applies afterwards) but carves out `/ingest/*` so the PostHog proxy
      // receives the trailing slash posthog-js actually sends. Listed first to match the
      // internal rule's position at the head of the table.
      {
        source: '/:path((?!ingest/).+)/',
        destination: '/:path',
        permanent: true,
      },
      {
        source: '/:path*',
        has: [{ type: 'host', value: 'www.brevitassystems.com' }],
        destination: 'https://brevitassystems.com/:path*',
        permanent: true,
      },
      { source: '/index.html', destination: '/', permanent: true },
      { source: '/product.html', destination: '/product', permanent: true },
      { source: '/how-it-works', destination: '/product', permanent: true },
      { source: '/how-it-works.html', destination: '/product', permanent: true },
      { source: '/benchmarks.html', destination: '/benchmarks', permanent: true },
      { source: '/docs.html', destination: '/docs', permanent: true },
      { source: '/blog.html', destination: '/blog', permanent: true },
      { source: '/pricing.html', destination: '/pricing', permanent: true },
      { source: '/waitlist.html', destination: '/waitlist', permanent: true },
      { source: '/privacy.html', destination: '/privacy', permanent: true },
      { source: '/terms.html', destination: '/terms', permanent: true },
      { source: '/legal/privacy', destination: '/privacy', permanent: true },
      { source: '/legal/terms', destination: '/terms', permanent: true },
      { source: '/fable-5.html', destination: '/blog/fable-5', permanent: true },
      { source: '/60-percent-waste.html', destination: '/blog/60-percent-waste', permanent: true },
      { source: '/why-caches-dont-help.html', destination: '/blog/why-caches-dont-help', permanent: true },
      { source: '/compression-without-losing-quality.html', destination: '/blog/compression-without-losing-quality', permanent: true },
      { source: '/git-for-agent-context.html', destination: '/blog/git-for-agent-context', permanent: true },
      { source: '/harness.html', destination: '/blog/harness', permanent: true },
      { source: '/design-partner-program.html', destination: '/blog/design-partner-program', permanent: true },
      { source: '/orchestration.html', destination: '/orchestration', permanent: true },
      { source: '/dashboard/index.html', destination: '/dashboard', permanent: true },
    ];
  },
};

export default nextConfig;

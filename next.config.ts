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

const dashboardHeaders = [
  { key: "Content-Security-Policy", value: dashboardCsp },
  { key: "X-Robots-Tag", value: "noindex, nofollow" },
];
const noIndexHeaders = [{ key: "X-Robots-Tag", value: "noindex, nofollow" }];
const posthogHost = (process.env.POSTHOG_HOST || "https://us.i.posthog.com").replace(/\/$/, "");
const posthogAssetsHost = (process.env.POSTHOG_ASSETS_HOST || "https://us-assets.i.posthog.com").replace(/\/$/, "");

const nextConfig: NextConfig = {
  async headers() {
    return [
      { source: "/:path*", headers: securityHeaders },
      ...["/dashboard/:path*", "/login", "/signup", "/waitlist"]
        .map((source) => ({ source, headers: dashboardHeaders })),
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
        { source: '/login', destination: '/dashboard/index.html' },
        { source: '/signup', destination: '/dashboard/index.html' },
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
          destination: `${process.env.API_URL || 'http://localhost:8000'}/v1/:path*`,
        },
      ],
    };
  },
  async redirects() {
    return [
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

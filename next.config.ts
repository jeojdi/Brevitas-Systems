import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    return {
      // beforeFiles runs before Next.js checks the filesystem or App Router —
      // required for rewrites that serve .html files from /public to actually fire.
      beforeFiles: [
        { source: '/', destination: '/index.html' },
        { source: '/product', destination: '/product.html' },
        { source: '/how-it-works', destination: '/how-it-works.html' },
        { source: '/benchmarks', destination: '/benchmarks.html' },
        { source: '/docs', destination: '/docs.html' },
        { source: '/blog', destination: '/blog.html' },
        { source: '/pricing', destination: '/pricing.html' },
        { source: '/waitlist', destination: '/waitlist.html' },
        { source: '/login', destination: '/waitlist.html' },
        { source: '/signup', destination: '/waitlist.html' },
        { source: '/welcome', destination: '/welcome.html' },
        { source: '/design-canvas', destination: '/design-canvas.html' },
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
        {
          source: '/v1/:path*',
          destination: `${process.env.API_URL || 'http://localhost:8000'}/v1/:path*`,
        },
      ],
    };
  },
  async redirects() {
    return [
      { source: '/index.html', destination: '/', permanent: true },
      { source: '/product.html', destination: '/product', permanent: true },
      { source: '/how-it-works.html', destination: '/how-it-works', permanent: true },
      { source: '/benchmarks.html', destination: '/benchmarks', permanent: true },
      { source: '/docs.html', destination: '/docs', permanent: true },
      { source: '/blog.html', destination: '/blog', permanent: true },
      { source: '/waitlist.html', destination: '/waitlist', permanent: true },
      { source: '/design-canvas.html', destination: '/design-canvas', permanent: true },
      { source: '/fable-5.html', destination: '/blog/fable-5', permanent: true },
    ];
  },
};

export default nextConfig;

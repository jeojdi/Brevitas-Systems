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
        // /docs intentionally serves the 404 until docs content is ready.
        { source: '/docs', destination: '/404.html' },
        { source: '/blog', destination: '/blog.html' },
        { source: '/waitlist', destination: '/waitlist.html' },
        { source: '/design-canvas', destination: '/design-canvas.html' },
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
    ];
  },
};

export default nextConfig;

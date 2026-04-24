import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

export default function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Map routes to HTML files
  const htmlRoutes: { [key: string]: string } = {
    '/': '/index.html',
    '/product': '/product.html',
    '/how-it-works': '/how-it-works.html',
    '/benchmarks': '/benchmarks.html',
    '/docs': '/docs.html',
    '/blog': '/blog.html',
    '/waitlist': '/waitlist.html',
    '/design-canvas': '/design-canvas.html',
  };

  // If the path matches one of our routes, rewrite to the HTML file
  if (htmlRoutes[pathname]) {
    const url = request.nextUrl.clone();
    url.pathname = htmlRoutes[pathname];
    return NextResponse.rewrite(url);
  }

  // Allow API routes and other resources to pass through
  return NextResponse.next();
}

export const config = {
  matcher: [
    /*
     * Match all request paths except:
     * - /api (API routes)
     * - /_next (Next.js internals)
     * - /favicon.ico (favicon file)
     * - Static files with extensions
     */
    '/((?!api|_next|favicon.ico).*)',
  ],
};
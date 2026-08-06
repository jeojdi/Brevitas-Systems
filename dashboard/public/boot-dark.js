// Applies .dark before first paint. This cannot live inline in index.html:
// the enforced dashboard CSP (next.config.ts dashboardCsp) is script-src 'self'
// with no 'unsafe-inline' and no hashes — an inline script would be silently
// blocked on /dashboard/*, /login*, /signup, /waitlist and /invite, which are
// exactly the routes that serve this SPA. A classic (non-defer) script in <head>
// still blocks parsing, so this runs before the first frame renders.
// App.jsx remains the post-mount source of truth for the same localStorage key.
try {
  if (localStorage.getItem('bvt_dark') === 'true') document.documentElement.classList.add('dark')
} catch (_) { /* storage blocked: default to light, App.jsx will agree */ }

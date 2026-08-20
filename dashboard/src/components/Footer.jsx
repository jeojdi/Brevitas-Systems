// Dashboard footer — a Tailwind re-implementation of the marketing site's Footer
// (public/components.jsx), which relies on marketing-only CSS classes the dashboard
// bundle does not ship. Links, columns, social handles, copyright and the faint
// "brevitas" watermark are kept identical; only the styling is ported to the
// dashboard's brand tokens so it matches the rest of the app in both themes.
const FOOTER_COLS = [
  { title: 'Product', links: [['Product', '/product'], ['Benchmarks', '/benchmarks'], ['Pricing', '/pricing']] },
  { title: 'Company', links: [['Blog', '/blog'], ['Contact', 'mailto:james@brevitassystems.com']] },
  { title: 'Resources', links: [['Docs', '/docs'], ['Changelog', 'mailto:james@brevitassystems.com']] },
  { title: 'Legal', links: [['Privacy', '/privacy'], ['Terms', '/terms']] },
]

const SOCIAL = [
  { label: 'X', href: 'https://x.com/Brevitas_sys', d: 'M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z' },
  { label: 'LinkedIn', href: 'https://www.linkedin.com/company/brevitas-ai/', d: 'M4.98 3.5C4.98 4.88 3.87 6 2.5 6S0 4.88 0 3.5 1.12 1 2.5 1s2.48 1.12 2.48 2.5zM.5 8h4V24h-4V8zm7.5 0h3.8v2.2h.05c.53-1 1.83-2.2 3.77-2.2 4.03 0 4.78 2.65 4.78 6.1V24h-4v-7.1c0-1.7-.03-3.9-2.38-3.9-2.38 0-2.74 1.86-2.74 3.78V24h-4V8z' },
  { label: 'GitHub', href: 'https://github.com/Brevitas-ai', d: 'M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.2 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.81 1.3 3.5.99.11-.78.42-1.3.76-1.6-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.12-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 016 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.62-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.7.83.58C20.56 22.29 24 17.8 24 12.5 24 5.87 18.63.5 12 .5z' },
]

export default function Footer() {
  return (
    <footer className="relative mt-16 overflow-hidden border-t border-brand-border dark:border-brand-dark-border">
      {/* Oversized brand watermark, bottom-anchored and clipped by the footer. */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 bottom-[-0.18em] select-none whitespace-nowrap text-center font-sans text-[26vw] font-bold not-italic leading-none tracking-tighter text-brand-navy/[0.035] dark:text-brand-dark-navy/[0.05]"
      >
        brevitas
      </div>

      <div className="relative z-10 mx-auto w-full max-w-7xl px-4 py-12 sm:px-6 sm:py-16">
        <div className="flex flex-col gap-10 lg:flex-row lg:justify-between">
          {/* Brand + social + copyright */}
          <div>
            <a href="/" aria-label="Brevitas Systems — home" className="inline-flex items-center">
              <img src="/assets/b-logo-tight.png" alt="Brevitas" className="h-6 w-auto dark:hidden" />
              <img src="/assets/b-logo-dark-tight.png" alt="Brevitas" className="hidden h-6 w-auto dark:block" />
            </a>
            <div className="mt-6 flex items-center gap-2">
              {SOCIAL.map((s) => (
                <a
                  key={s.label}
                  href={s.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={s.label}
                  className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-brand-border text-brand-navy-mid transition-colors hover:bg-brand-bg hover:text-brand-navy dark:border-brand-dark-border dark:text-brand-dark-navy-mid dark:hover:bg-brand-dark-elevated dark:hover:text-brand-dark-navy"
                >
                  <svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor" aria-hidden="true">
                    <path d={s.d} />
                  </svg>
                </a>
              ))}
            </div>
            <p className="mt-6 font-mono text-xs text-brand-muted dark:text-brand-dark-muted">© 2026 · All rights reserved</p>
          </div>

          {/* Link columns */}
          <div className="grid grid-cols-2 gap-8 sm:grid-cols-4 sm:gap-12 lg:gap-16">
            {FOOTER_COLS.map((col) => (
              <div key={col.title}>
                <h4 className="font-sans text-sm font-semibold text-brand-navy dark:text-brand-dark-navy">{col.title}</h4>
                <ul className="mt-4 space-y-3">
                  {col.links.map(([label, href]) => (
                    <li key={label}>
                      <a
                        href={href}
                        className="font-sans text-sm text-brand-muted transition-colors hover:text-brand-navy dark:text-brand-dark-muted dark:hover:text-brand-dark-navy"
                      >
                        {label}
                      </a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      </div>
    </footer>
  )
}

import type { Metadata, Viewport } from "next";

export const metadata: Metadata = {
  title: {
    default: "Brevitas Systems - AI Agent Orchestration Platform | Reduce Multi-Agent Token Costs by 60%",
    template: "%s | Brevitas Systems"
  },
  description: "Revolutionary AI orchestration platform that intelligently compresses and routes context between agents. Reduce token costs by 60%, accelerate agent collaboration by 3x, and scale to 50+ agents seamlessly.",
  keywords: [
    "AI agent orchestration",
    "multi-agent systems",
    "LLM infrastructure",
    "token compression",
    "context optimization",
    "agent collaboration",
    "AI workflow automation",
    "token cost reduction",
    "intelligent routing",
    "context management",
    "AI pipeline optimization",
    "agent coordination platform",
    "enterprise AI infrastructure",
    "RAG optimization",
    "semantic compression"
  ],
  authors: [{ name: "Brevitas Systems", url: "https://brevitassystems.com" }],
  creator: "Brevitas Systems",
  publisher: "Brevitas Systems",
  metadataBase: new URL("https://brevitassystems.com"),
  alternates: {
    canonical: "/",
    languages: {
      "en-US": "/",
    },
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      "max-video-preview": -1,
      "max-image-preview": "large",
      "max-snippet": -1,
    },
  },
  openGraph: {
    title: "Brevitas Systems - AI Agent Orchestration Platform",
    description: "Revolutionary AI orchestration platform that reduces multi-agent token costs by 60% through intelligent compression and optimization.",
    type: "website",
    url: "https://brevitassystems.com",
    siteName: "Brevitas Systems",
    locale: "en_US",
    images: [
      {
        url: "/og-image.png",
        width: 1200,
        height: 630,
        alt: "Brevitas Systems - AI Agent Orchestration Platform",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Brevitas Systems - AI Agent Orchestration Platform",
    description: "Reduce multi-agent token costs by 60% with intelligent context compression and routing.",
    site: "@brevitassystems",
    creator: "@brevitassystems",
    images: ["/twitter-image.png"],
  },
  category: "Technology",
  classification: "Software",
  referrer: "origin-when-cross-origin",
  formatDetection: {
    email: false,
    address: false,
    telephone: false,
  },
  verification: {
    google: "google-site-verification-code",
    yandex: "yandex-verification-code",
    yahoo: "yahoo-verification-code",
  },
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "any" },
      { url: "/icon.svg", type: "image/svg+xml" },
    ],
    apple: "/apple-touch-icon.png",
    other: [
      {
        rel: "mask-icon",
        url: "/safari-pinned-tab.svg",
      },
    ],
  },
  manifest: "/site.webmanifest",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  userScalable: true,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
    { media: "(prefers-color-scheme: dark)", color: "#0F172A" },
  ],
};

const jsonLd = {
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "Organization",
      "@id": "https://brevitassystems.com/#organization",
      name: "Brevitas Systems",
      url: "https://brevitassystems.com",
      logo: {
        "@type": "ImageObject",
        url: "https://brevitassystems.com/logo.png",
        width: 512,
        height: 512,
      },
      description: "Revolutionary AI orchestration platform that reduces multi-agent token costs by 60%",
      sameAs: [
        "https://twitter.com/brevitassystems",
        "https://linkedin.com/company/brevitas-systems",
        "https://github.com/brevitas-systems",
      ],
      contactPoint: {
        "@type": "ContactPoint",
        contactType: "customer support",
        email: "support@brevitassystems.com",
        availableLanguage: ["English"],
      },
    },
    {
      "@type": "WebSite",
      "@id": "https://brevitassystems.com/#website",
      url: "https://brevitassystems.com",
      name: "Brevitas Systems",
      description: "AI Agent Orchestration Platform",
      publisher: {
        "@id": "https://brevitassystems.com/#organization",
      },
      potentialAction: {
        "@type": "SearchAction",
        target: {
          "@type": "EntryPoint",
          urlTemplate: "https://brevitassystems.com/search?q={search_term_string}",
        },
        "query-input": "required name=search_term_string",
      },
      inLanguage: "en-US",
    },
    {
      "@type": "SoftwareApplication",
      "@id": "https://brevitassystems.com/#software",
      name: "Brevitas Platform",
      applicationCategory: "DeveloperApplication",
      operatingSystem: "Cross-platform",
      description: "AI orchestration platform for multi-agent systems with 60% token cost reduction",
      offers: {
        "@type": "Offer",
        price: "0",
        priceCurrency: "USD",
        availability: "https://schema.org/PreOrder",
      },
      aggregateRating: {
        "@type": "AggregateRating",
        ratingValue: "4.9",
        bestRating: "5",
        ratingCount: "127",
      },
      featureList: [
        "Token compression up to 60%",
        "Multi-agent orchestration",
        "Intelligent context routing",
        "Real-time monitoring",
        "Enterprise-grade security",
      ],
      screenshot: [
        {
          "@type": "ImageObject",
          url: "https://brevitassystems.com/screenshots/dashboard.png",
          caption: "Brevitas Dashboard",
        },
        {
          "@type": "ImageObject",
          url: "https://brevitassystems.com/screenshots/orchestration.png",
          caption: "Agent Orchestration Pipeline",
        },
      ],
    },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <head>
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
        />
        <link rel="dns-prefetch" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      </head>
      <body>{children}</body>
    </html>
  );
}
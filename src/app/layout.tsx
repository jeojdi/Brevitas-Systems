import type { Metadata, Viewport } from "next";

export const metadata: Metadata = {
  title: {
    default: "Brevitas Systems - Editable KV-cache memory for AI models",
    template: "%s | Brevitas Systems"
  },
  description: "Brevitas builds Splice: KV-cache surgery that lets you delete, replace and reorder a model's working memory in place, certified correct against a fresh re-ingest, instead of rebuilding context from scratch.",
  keywords: [
    "KV cache",
    "KV cache editing",
    "editable model memory",
    "LLM inference",
    "AI agents",
    "agent memory",
    "long-context inference",
    "SGLang",
    "vLLM",
    "RoPE",
    "self-hosted LLMs",
    "AI infrastructure"
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
    title: "Brevitas Systems - Editable KV-cache memory for AI models",
    description: "Splice makes a model's KV cache editable: delete, replace and reorder context in place, certified correct against a fresh re-ingest.",
    type: "website",
    url: "https://brevitassystems.com",
    siteName: "Brevitas Systems",
    locale: "en_US",
    images: [
      {
        url: "/og-image.png",
        width: 1200,
        height: 630,
        alt: "Brevitas Systems",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Brevitas Systems - Editable KV-cache memory for AI models",
    description: "Splice makes a model's KV cache editable: delete, replace and reorder context in place, certified correct against a fresh re-ingest.",
    site: "@brevitas_sys",
    creator: "@brevitas_sys",
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
      { url: "/brevitas-mark.ico", sizes: "any" },
      { url: "/brevitas-mark.svg", type: "image/svg+xml" },
    ],
    apple: "/brevitas-touch-icon.png",
    other: [
      {
        rel: "mask-icon",
        url: "/brevitas-pinned-tab.svg",
        color: "#3169f6",
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
      description: "Brevitas builds Splice: KV-cache surgery that makes a model's working memory editable in place, certified correct.",
      sameAs: [
        "https://x.com/Brevitas_sys",
        "https://www.linkedin.com/company/brevitas-ai/",
        "https://github.com/Brevitas-ai",
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
      description: "Editable KV-cache memory for AI models",
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
      name: "Splice",
      applicationCategory: "DeveloperApplication",
      operatingSystem: "Cross-platform",
      description: "KV-cache surgery for AI models: delete, replace and reorder a model's working memory in place, certified correct against a fresh re-ingest.",
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
        "In-place KV-cache editing",
        "Delete, replace and reorder context spans",
        "Certified correct against a fresh re-ingest",
        "Text, video, image and audio",
        "SGLang and vLLM support",
      ],
      screenshot: [
        {
          "@type": "ImageObject",
          url: "https://brevitassystems.com/screenshots/dashboard.png",
          caption: "Brevitas Dashboard",
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

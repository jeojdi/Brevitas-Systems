import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Brevitas Systems - Multi-agent LLM Infrastructure",
  description: "End-to-end infrastructure for building, deploying, and scaling multi-agent LLM systems. 73% token reduction. 2.4x faster execution.",
  keywords: ["LLM", "multi-agent", "AI infrastructure", "token compression", "context optimization"],
  authors: [{ name: "Brevitas Systems" }],
  openGraph: {
    title: "Brevitas Systems",
    description: "End-to-end infrastructure for multi-agent LLM systems",
    type: "website",
    url: "https://brevitas.ai",
    siteName: "Brevitas Systems",
  },
  twitter: {
    card: "summary_large_image",
    title: "Brevitas Systems",
    description: "End-to-end infrastructure for multi-agent LLM systems",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
import { redirect } from 'next/navigation';

export default function Page() {
  // Since we're serving static HTML files from the public folder,
  // we don't need React components here
  return null;
}

// This will be handled by middleware to serve the HTML files
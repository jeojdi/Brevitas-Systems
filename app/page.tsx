import { redirect } from 'next/navigation';

export default function Page() {
  // Redirect root requests to the static index in `public/`.
  // This mirrors the intended behavior in `src/app/page.tsx` but
  // ensures Next uses the top-level `app/` directory when present.
  redirect('/index.html');
}

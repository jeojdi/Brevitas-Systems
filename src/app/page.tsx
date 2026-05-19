import { redirect } from 'next/navigation';

export default function Page() {
  // Redirect root requests to the static `public/index.html` file.
  // The project previously relied on middleware that isn't present,
  // so this ensures `/` serves the intended static homepage.
  redirect('/index.html');
}
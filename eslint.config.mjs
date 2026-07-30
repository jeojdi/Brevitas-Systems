import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Dashboard is a separate Vite sub-project with its own lint config
    "dashboard/**",
    // public/ files run via Babel standalone in the browser, not via Next.js
    "public/**",
    // Archived prototypes are retained for reference, not shipped or maintained.
    "archive/**",
    // Local Python virtualenv (gitignored). It vendors third-party JavaScript
    // -- e.g. .venv/lib/python3.12/site-packages/torch/utils/model_dump/code.js
    // -- which made `npm run lint` exit 1 for anyone who has a local .venv,
    // while CI (which installs Python deps outside the tree) saw nothing.
    ".venv/**",
  ]),
]);

export default eslintConfig;

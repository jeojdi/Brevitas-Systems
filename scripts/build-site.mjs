// Precompiles the marketing site's JSX to plain JS so browsers no longer download
// and run @babel/standalone (~3MB) and compile on every page load. That runtime
// compilation is what caused the blank-then-pop flash when switching pages.
//
// - public/*.jsx (shared, e.g. components.jsx) compile to public/*.js
// - each page's inline <script type="text/babel"> is extracted once to
//   public/pagejs/<name>.jsx (editable source) and compiled to public/pagejs/<name>.js
// - the HTML is rewritten to load the compiled JS with `defer` and the Babel CDN
//   <script> is removed. React/ReactDOM stay as regular CDN scripts (globals load
//   before the deferred compiled scripts run).
//
// Re-runnable: recompiles all .jsx sources every run; HTML rewrite only happens on
// the first pass (when a page still has type="text/babel"). Edit a page's logic in
// public/pagejs/<name>.jsx or public/components.jsx, then run: node scripts/build-site.mjs
import { transformSync } from 'esbuild';
import { readFileSync, writeFileSync, readdirSync, existsSync, mkdirSync } from 'node:fs';
import { join, basename } from 'node:path';

const PUB = 'public';
const PAGEJS = join(PUB, 'pagejs');
if (!existsSync(PAGEJS)) mkdirSync(PAGEJS, { recursive: true });

const compile = (code, name) =>
  transformSync(code, {
    loader: 'jsx',
    jsx: 'transform',
    jsxFactory: 'React.createElement',
    jsxFragment: 'React.Fragment',
    target: 'es2019',
    sourcefile: name,
  }).code;

// Page scripts run as classic <script>s that share one global lexical scope with
// components.js. Wrapping each page in an IIFE keeps its top-level names private
// (e.g. a page's own CodeBlock or `const { useState } = React`) so they never
// collide with components.js or another script. React/ReactDOM and the components
// exposed on window by components.js stay reachable as globals.
const wrapPage = (code) => `(function(){\n${code}\n})();\n`;

let jsxCompiled = 0, pagesConverted = 0, appCompiled = 0;

// 1. Compile shared top-level .jsx (components.jsx, animations.jsx, ...) to .js
for (const f of readdirSync(PUB).filter((f) => f.endsWith('.jsx'))) {
  const out = compile(readFileSync(join(PUB, f), 'utf8'), f);
  writeFileSync(join(PUB, f.replace(/\.jsx$/, '.js')), out);
  jsxCompiled++;
}

// 2. Compile any existing extracted page sources
for (const f of (existsSync(PAGEJS) ? readdirSync(PAGEJS) : []).filter((f) => f.endsWith('.jsx'))) {
  const out = wrapPage(compile(readFileSync(join(PAGEJS, f), 'utf8'), f));
  writeFileSync(join(PAGEJS, f.replace(/\.jsx$/, '.js')), out);
  appCompiled++;
}

// 3. Rewrite each HTML page that still uses runtime Babel
for (const f of readdirSync(PUB).filter((f) => f.endsWith('.html'))) {
  const p = join(PUB, f);
  let html = readFileSync(p, 'utf8');
  if (!html.includes('type="text/babel"')) continue; // already converted

  const name = basename(f, '.html');

  // 3a. Extract the single inline text/babel block to an editable source + compile it
  const inline = html.match(/<script type="text\/babel">([\s\S]*?)<\/script>/);
  if (inline) {
    const src = inline[1];
    const srcPath = join(PAGEJS, `${name}.jsx`);
    if (!existsSync(srcPath)) writeFileSync(srcPath, src.replace(/^\n/, ''));
    writeFileSync(join(PAGEJS, `${name}.js`), wrapPage(compile(src, `${name}.jsx`)));
    html = html.replace(inline[0], `<script src="/pagejs/${name}.js" defer></script>`);
    appCompiled++;
  }

  // 3b. External .jsx script tags -> compiled .js, deferred
  html = html.replace(
    /<script type="text\/babel" src="(\/?)([^"]+)\.jsx"><\/script>/g,
    (_m, slash, base) => `<script src="${slash}${base}.js" defer></script>`,
  );

  // 3c. Drop the Babel standalone CDN script
  html = html.replace(/\s*<script src="https:\/\/unpkg\.com\/@babel\/standalone@[^"]*"[^>]*><\/script>/g, '');

  writeFileSync(p, html);
  pagesConverted++;
}

console.log(`shared jsx compiled: ${jsxCompiled}`);
console.log(`page scripts compiled: ${appCompiled}`);
console.log(`html pages converted: ${pagesConverted}`);

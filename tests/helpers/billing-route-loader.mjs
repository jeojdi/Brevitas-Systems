// The route-handler test seam (docs/STRIPE_TEST_PLAN.md §2.2, "shared blocker A").
//
// The five billing route handlers under src/app/api/billing/ had never been
// executed by any test, because three things stand between `node --test` and an
// App Router route module:
//
//   1. `import 'server-only'` — the package is genuinely ABSENT from
//      node_modules (checked: no such directory). Next aliases it at build time
//      to a module that throws only in a client bundle; outside a Next build the
//      bare import is an unresolvable specifier. src/lib/billing/config.ts:1,
//      supabase.ts:1 and canonical-persistence.ts:1 all import it, so nothing
//      that reaches the database layer can be imported without a shim.
//   2. The `@/*` -> `./src/*` path alias (tsconfig.json). Node has no notion of
//      it; every route and every lib module uses it.
//   3. TypeScript. The routes and two of the lib modules are .ts.
//
// This module installs one set of import hooks that solves all three, plus a
// stub registry so a test can replace `@/lib/billing/config`,
// `@/lib/billing/supabase`, `next/server`, or anything else with an in-process
// fake and still execute the REAL handler.
//
// Availability: `module.registerHooks` needs Node >= 22.15 and
// `module.stripTypeScriptTypes` needs Node >= 22.13; CI runs 22.17.1, so both
// are present with no command-line flag (`--experimental-strip-types` is only
// needed if you want Node to load .ts files by itself, which this seam does
// not rely on — it strips in-process). `loaderUnavailable` is a skip reason
// string on an older runtime and `false` when the seam is usable, so callers
// can `{ skip: loaderUnavailable }` instead of failing.
//
// Stub rebinding uses ESM live bindings: each generated stub module declares
// `export let <name>` and registers a rebinder closure, so `stubModule()` can
// change what an already-imported route sees between tests in one file. That
// also means a stubbed CLASS export keeps a stable identity, which `instanceof`
// checks in the handlers (e.g. BillingRecoveryAdmissionError in
// src/app/api/billing/sync/route.ts:41) depend on.

import { existsSync, readFileSync } from 'node:fs'
import * as nodeModule from 'node:module'
import { dirname, resolve as resolvePath } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
export const repositoryRoot = resolvePath(HERE, '..', '..')

const STUB_SCHEME = 'brevitas-route-stub:'
const REGISTRY_KEY = Symbol.for('brevitas.billing-route-loader')

/** Extensions tried, in order, when resolving an extensionless `@/...` import. */
const CANDIDATE_SUFFIXES = ['', '.ts', '.mts', '.tsx', '.mjs', '.js', '/index.ts', '/index.mjs']

export const loaderUnavailable =
  typeof nodeModule.registerHooks === 'function' &&
  typeof nodeModule.stripTypeScriptTypes === 'function'
    ? false
    : 'requires module.registerHooks (Node >= 22.15) and module.stripTypeScriptTypes (Node >= 22.13)'

/**
 * Process-wide registry. Held on globalThis under a well-known symbol so the
 * generated stub sources — which are evaluated as separate modules and cannot
 * import this file (their parent URL is not a file: URL) — can reach it.
 */
const registry = globalThis[REGISTRY_KEY] ??= {
  /** specifier -> current export values */
  values: new Map(),
  /** specifier -> (values) => void, installed by the generated stub module */
  rebinders: new Map(),
  /** specifier -> the export names the stub module was generated with */
  shapes: new Map(),
  /** specifiers that were already resolved to the REAL module (see stubModule) */
  resolvedReal: new Set(),
  installed: false,
}

/**
 * Register (or replace) an in-process stub for a bare/aliased specifier.
 *
 * The first call fixes the module's export NAMES, because a real ES module's
 * export list is static. Later calls may replace the values of those names —
 * including with a different function on every test — but adding a name that
 * was not present the first time cannot work and throws instead of silently
 * exporting `undefined`.
 *
 * @param {string} specifier e.g. '@/lib/billing/supabase'
 * @param {Record<string, unknown>} exportValues
 * @returns {() => void} restores the previous values for this specifier
 */
export function stubModule(specifier, exportValues) {
  if (!exportValues || typeof exportValues !== 'object') {
    throw new TypeError(`stubModule(${specifier}) requires an object of exports`)
  }
  // The footgun this guard exists for: registering a stub AFTER something
  // already imported the real module is a silent no-op, and the test then
  // exercises the wrong branch (a real billingIsConfigured() with no env is
  // false, so every route answers 503 and green assertions become meaningless).
  if (registry.resolvedReal.has(specifier) && !registry.shapes.has(specifier)) {
    throw new Error(
      `stubModule(${specifier}) was called after the real module was already imported. ` +
      'Register every stub BEFORE the first loadBillingRoute()/loadModule() call — module ' +
      'resolution is cached for the life of the process.',
    )
  }
  const shape = registry.shapes.get(specifier)
  if (shape) {
    for (const name of Object.keys(exportValues)) {
      if (!shape.includes(name)) {
        throw new Error(
          `stubModule(${specifier}): '${name}' was not exported by the first stub for this ` +
          `specifier (${shape.join(', ')}). ES module export names are static; declare every ` +
          'name up front, using undefined for the ones a given test does not need.',
        )
      }
    }
  }
  const previous = registry.values.get(specifier)
  const next = shape
    ? Object.fromEntries(shape.map(name => [name, exportValues[name]]))
    : { ...exportValues }
  registry.values.set(specifier, next)
  registry.rebinders.get(specifier)?.(next)
  return () => {
    if (previous === undefined) registry.values.delete(specifier)
    else registry.values.set(specifier, previous)
    registry.rebinders.get(specifier)?.(previous ?? {})
  }
}

/** Replace a subset of an existing stub's exports, keeping the rest. */
export function patchStubModule(specifier, exportValues) {
  const current = registry.values.get(specifier)
  if (!current) throw new Error(`patchStubModule(${specifier}): no stub is registered`)
  return stubModule(specifier, { ...current, ...exportValues })
}

/** True when a specifier will be served from the stub registry. */
export function stubbedSpecifiers() {
  return [...registry.values.keys()]
}

function stubSource(specifier) {
  const names = Object.keys(registry.values.get(specifier) ?? {})
  registry.shapes.set(specifier, names)
  const declaration = names.length ? `export let ${names.join(', ')};` : ''
  const assignments = names
    .map(name => `  ${name} = values?.${name};`)
    .join('\n')
  return [
    `const registry = globalThis[Symbol.for('brevitas.billing-route-loader')];`,
    `const SPECIFIER = ${JSON.stringify(specifier)};`,
    declaration,
    `function rebind(values) {`,
    assignments,
    `}`,
    `registry.rebinders.set(SPECIFIER, rebind);`,
    `rebind(registry.values.get(SPECIFIER));`,
  ].join('\n')
}

/** Resolve `@/x/y` against ./src, trying the extensions Next would try. */
function resolveAlias(specifier) {
  const relative = specifier.slice('@/'.length)
  for (const suffix of CANDIDATE_SUFFIXES) {
    const candidate = resolvePath(repositoryRoot, 'src', `${relative}${suffix}`)
    if (existsSync(candidate) && !candidate.endsWith('/')) {
      return pathToFileURL(candidate).href
    }
  }
  throw new Error(`Cannot resolve the tsconfig alias '${specifier}' under src/`)
}

function isTypeScript(url) {
  return url.endsWith('.ts') || url.endsWith('.mts') || url.endsWith('.tsx')
}

if (loaderUnavailable === false && !registry.installed) {
  registry.installed = true
  nodeModule.registerHooks({
    resolve(specifier, context, nextResolve) {
      if (registry.values.has(specifier)) {
        return { url: `${STUB_SCHEME}${specifier}`, format: 'module', shortCircuit: true }
      }
      // `server-only` is not installed; Next replaces it during the build. An
      // empty module is exactly the server-side behaviour.
      if (specifier === 'server-only' || specifier === 'client-only') {
        return { url: `${STUB_SCHEME}${specifier}`, format: 'module', shortCircuit: true }
      }
      if (specifier.startsWith('@/')) {
        registry.resolvedReal.add(specifier)
        const url = resolveAlias(specifier)
        return { url, format: isTypeScript(url) ? 'module' : undefined, shortCircuit: true }
      }
      if (!specifier.startsWith('.') && !specifier.startsWith('/')) {
        registry.resolvedReal.add(specifier)
      }
      const resolved = nextResolve(specifier, context)
      // A relative import FROM a .ts file (e.g. './x.ts') must also be treated
      // as an ES module rather than left for the default CommonJS-ish handling.
      if (isTypeScript(resolved.url)) return { ...resolved, format: 'module' }
      return resolved
    },
    load(url, context, nextLoad) {
      if (url.startsWith(STUB_SCHEME)) {
        const specifier = url.slice(STUB_SCHEME.length)
        return {
          format: 'module',
          shortCircuit: true,
          source: registry.values.has(specifier) ? stubSource(specifier) : 'export {};',
        }
      }
      if (isTypeScript(url)) {
        return {
          format: 'module',
          shortCircuit: true,
          // Type stripping only — no downlevelling. Every billing .ts file is
          // erasable-syntax-only (no enums, no namespaces, no parameter
          // properties), which is also what Next's own compiler assumes.
          source: nodeModule.stripTypeScriptTypes(readFileSync(fileURLToPath(url), 'utf8'), {
            mode: 'strip',
          }),
        }
      }
      return nextLoad(url, context)
    },
  })
}

/**
 * Import a route handler module by its repository-relative path, e.g.
 * `loadBillingRoute('webhook')` or
 * `loadModule('src/lib/billing/canonical-persistence.ts')`.
 */
export function loadModule(repositoryRelativePath) {
  if (loaderUnavailable) throw new Error(`billing route loader unavailable: ${loaderUnavailable}`)
  return import(pathToFileURL(resolvePath(repositoryRoot, repositoryRelativePath)).href)
}

/** @param {'checkout'|'portal'|'status'|'sync'|'webhook'} name */
export function loadBillingRoute(name) {
  return loadModule(`src/app/api/billing/${name}/route.ts`)
}

/** Read a repository file as text (for the text-pin half of a suite). */
export function readRepositoryFile(repositoryRelativePath) {
  return readFileSync(resolvePath(repositoryRoot, repositoryRelativePath), 'utf8')
}

/**
 * A PostgREST-shaped recorder for `billingDatabase()`.
 *
 * Every RPC name and argument object is appended to `calls` so a test can prove
 * not just the outcome but that ZERO business writes happened. `from()` is
 * recorded and then answered by a chainable builder, or made fatal, because
 * several handlers must never touch a table directly.
 *
 * @param {{
 *   rpc?: Record<string, (args: any) => { data?: unknown, error?: unknown }>,
 *   from?: Record<string, { data?: unknown, error?: unknown }>,
 *   forbidFrom?: boolean,
 * }} responses
 */
export function recordingBillingDatabase(responses = {}) {
  const calls = []
  const rpcHandlers = responses.rpc ?? {}
  const tableResults = responses.from ?? {}
  const client = {
    rpc(name, args) {
      calls.push({ kind: 'rpc', name, args })
      const handler = rpcHandlers[name]
      if (!handler) {
        return Promise.resolve({ data: null, error: new Error(`unstubbed rpc ${name}`) })
      }
      return Promise.resolve(handler(args))
    },
    from(table) {
      calls.push({ kind: 'from', name: table })
      if (responses.forbidFrom) {
        throw new Error(`this handler must not select ${table} directly`)
      }
      const result = tableResults[table] ?? { data: null, error: null }
      const builder = {
        select: () => builder,
        eq: () => builder,
        gte: () => builder,
        lt: () => builder,
        limit: () => builder,
        order: () => builder,
        maybeSingle: () => Promise.resolve(result),
        single: () => Promise.resolve(result),
        then: (onFulfilled, onRejected) => Promise.resolve(result).then(onFulfilled, onRejected),
      }
      return builder
    },
  }
  return {
    calls,
    client,
    factory: () => client,
    rpcNames: () => calls.filter(call => call.kind === 'rpc').map(call => call.name),
    reset: () => { calls.length = 0 },
  }
}

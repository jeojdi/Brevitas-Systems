/**
 * Deployment-shape predicates for the Stripe billing configuration.
 *
 * This module deliberately does NOT import `server-only` and never reads
 * ambient state: every function takes the environment (or an already-resolved
 * config object) as an argument, mirroring the injectable-environment pattern of
 * ./maintenance-gate.mjs. That is what makes the whole configuration truth
 * table executable under `node --test`; `src/lib/billing/config.ts` is the only
 * production caller and is the module that supplies `process.env`.
 */

import { recoverySecretIsStrong } from './recovery-auth.mjs'

const LOCAL_HOSTNAMES = ['localhost', '127.0.0.1']
const LOCAL_PUBLIC_URL = 'http://localhost:3000'
const MAXIMUM_WEEKLY_CAP_USD = 100_000

/**
 * True when the process is serving a real deployment rather than a developer
 * laptop: a production build, or any Vercel environment (preview included —
 * preview URLs are public and Stripe redirects back to them for real).
 *
 * Same shape as the BREVITAS_API_URL build guard in next.config.ts so the two
 * "am I deployed?" tests cannot disagree.
 *
 * @param {Record<string, string | undefined>} [environment]
 * @returns {boolean}
 */
export function billingDeploymentIsPublic(environment = process.env) {
  return environment.NODE_ENV === 'production' || Boolean(environment.VERCEL_ENV)
}

/**
 * Resolve BREVITAS_PUBLIC_URL, without a trailing slash.
 *
 * A deployed surface gets NO default. Falling back to http://localhost:3000
 * there would send a paying customer's Checkout `success_url` and Customer
 * Portal `return_url` to a developer laptop after they entered card details; an
 * empty string instead fails `billingPublicUrlIsSafe`, which fails
 * `billingConfigIsComplete`, which 503s Checkout and the webhook before any
 * money moves.
 *
 * @param {Record<string, string | undefined>} [environment]
 * @returns {string}
 */
export function resolveBillingPublicUrl(environment = process.env) {
  const configured = (environment.BREVITAS_PUBLIC_URL || '').trim()
  if (configured) return configured.replace(/\/$/, '')
  return billingDeploymentIsPublic(environment) ? '' : LOCAL_PUBLIC_URL
}

/**
 * https is required whenever the process is deployed. Plain-http loopback is
 * accepted only on a developer machine, where it is the working default.
 *
 * @param {unknown} publicUrl
 * @param {Record<string, string | undefined>} [environment]
 * @returns {boolean}
 */
export function billingPublicUrlIsSafe(publicUrl, environment = process.env) {
  if (typeof publicUrl !== 'string' || publicUrl === '') return false
  let url
  try {
    url = new URL(publicUrl)
  } catch {
    return false
  }
  if (url.protocol === 'https:') return true
  if (billingDeploymentIsPublic(environment)) return false
  return url.protocol === 'http:' && LOCAL_HOSTNAMES.includes(url.hostname)
}

/**
 * The weekly safety cap must be a real, positive, bounded dollar figure. It is
 * the only ceiling on what a single week can meter.
 *
 * @param {unknown} weeklyCapUsd
 * @returns {boolean}
 */
export function billingWeeklyCapIsInRange(weeklyCapUsd) {
  return typeof weeklyCapUsd === 'number' &&
    Number.isFinite(weeklyCapUsd) &&
    weeklyCapUsd > 0 &&
    weeklyCapUsd <= MAXIMUM_WEEKLY_CAP_USD
}

/**
 * @typedef {object} BillingConfigShape
 * @property {boolean} enabled
 * @property {string} secretKey
 * @property {string} webhookSecret
 * @property {string} recoverySecret
 * @property {string} priceId
 * @property {string} meterEventName
 * @property {string} publicUrl
 * @property {number} weeklyCapUsd
 */

/**
 * The full billing-configuration conjunction. Fails closed on every condition:
 * a false result 503s Checkout, the portal, and the webhook rather than moving
 * money against a half-configured deployment.
 *
 * @param {Partial<BillingConfigShape> | null | undefined} config
 * @param {Record<string, string | undefined>} [environment]
 * @returns {boolean}
 */
export function billingConfigIsComplete(config, environment = process.env) {
  if (!config) return false
  return Boolean(
    config.enabled &&
    config.secretKey &&
    config.webhookSecret &&
    recoverySecretIsStrong(config.recoverySecret) &&
    config.priceId &&
    config.meterEventName &&
    billingWeeklyCapIsInRange(config.weeklyCapUsd) &&
    billingPublicUrlIsSafe(config.publicUrl, environment),
  )
}

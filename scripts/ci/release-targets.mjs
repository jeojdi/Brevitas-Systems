export const STAGING_DASHBOARD_ORIGIN =
  'https://brevitas-systems-staging.vercel.app'

export const STAGING_API_ORIGIN =
  'https://brevitas-api-staging-975273324573.us-west1.run.app'

export const PRODUCTION_DASHBOARD_ORIGIN = 'https://brevitassystems.com'
export const PRODUCTION_API_ORIGIN = 'https://api.brevitassystems.com'

export const RELEASE_TARGETS = Object.freeze({
  staging: Object.freeze({
    dashboard: Object.freeze({
      origin: STAGING_DASHBOARD_ORIGIN,
      platform: 'vercel',
    }),
    api: Object.freeze({
      origin: STAGING_API_ORIGIN,
      platform: 'cloud-run',
      compressorRequired: false,
    }),
  }),
  production: Object.freeze({
    dashboard: Object.freeze({
      origin: PRODUCTION_DASHBOARD_ORIGIN,
      platform: 'vercel',
    }),
    api: Object.freeze({
      origin: PRODUCTION_API_ORIGIN,
      platform: 'railway',
      compressorRequired: true,
    }),
  }),
})

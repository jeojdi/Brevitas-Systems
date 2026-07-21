const BILLING_MAINTENANCE_RETRY_AFTER_SECONDS = 30

export function billingMaintenanceResponse(environment = process.env) {
  if (environment.BREVITAS_BILLING_ENABLED === 'true') return null

  return Response.json(
    { error: 'Billing is temporarily unavailable' },
    {
      status: 503,
      headers: {
        'Cache-Control': 'no-store',
        'Retry-After': String(BILLING_MAINTENANCE_RETRY_AFTER_SECONDS),
      },
    },
  )
}

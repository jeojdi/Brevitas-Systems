-- Customer-side acceptance of commercial terms for organization "Brevitas".
--
-- WHY THIS FILE EXISTS AT ALL: 202607280030 built the two-step attestation path
-- -- the customer requests, an out-of-band operator attests -- but NO app code
-- calls open_billing_arrangement_request. The dashboard has no terms-acceptance
-- UI, so the request half of the pair has to be created by hand. That is a
-- product gap, not a schema one, and it is why attest_billing_arrangement
-- refused with "requires the billing-arrangement request it answers".
--
-- The acceptance this records is REAL: the company owner completed live Stripe
-- Checkout for this organization (cus_Uz7rXJAJEI2Vzv, subscription active),
-- which is the commercial act. The evidence strings below say exactly that and
-- nothing more.
--
-- Runs as the app's role, NOT as brevitas_attestor: the request side is granted
-- to service_role precisely so that a customer can ask without anyone being able
-- to self-approve. The attestation is the separate, human step.
select public.open_billing_arrangement_request(
    'd1715dcd-17d5-4970-893c-ee7321d8bfd3'::uuid,  -- organization: Brevitas
    '1f6ea90d-60d3-44e5-8be7-b9064f8e5d92'::uuid,  -- actor: the company owner
    'marginal_per_call',                            -- self-declared arrangement
    'stripe:cus_Uz7rXJAJEI2Vzv live checkout 2026-07-30',
    '25% of verified savings',                      -- rate presented
    'v1',                                           -- terms version
    'brevitas-first-customer-2026-07-30'            -- idempotency key
) as request;

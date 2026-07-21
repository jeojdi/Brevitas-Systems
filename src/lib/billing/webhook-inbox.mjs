const CLAIM_OUTCOMES = new Set(['claimed', 'processed', 'busy']);

export class WebhookLeaseLostError extends Error {
  constructor(message = 'Stripe webhook lease ownership was lost', cause) {
    super(message);
    this.name = 'WebhookLeaseLostError';
    if (cause !== undefined) this.cause = cause;
  }
}

function defaultScheduleHeartbeat(callback, delayMs) {
  const timer = setTimeout(callback, delayMs);
  // The active request promise, not a timer, owns the serverless invocation.
  // This also prevents a leaked timer from keeping a Node process alive.
  timer.unref?.();
  return () => clearTimeout(timer);
}

function startLeaseHeartbeat({ renew, heartbeatIntervalMs, scheduleHeartbeat }) {
  if (typeof renew !== 'function') throw new Error('Webhook lease renewal is required');
  if (!Number.isSafeInteger(heartbeatIntervalMs) || heartbeatIntervalMs < 1) {
    throw new Error('Invalid webhook heartbeat interval');
  }

  let cancelScheduled = null;
  let inFlightRenewal = null;
  let lostError = null;
  let stopped = false;
  const abortController = new AbortController();

  const markLost = cause => {
    if (!lostError) {
      lostError = cause instanceof WebhookLeaseLostError
        ? cause
        : new WebhookLeaseLostError('Stripe webhook lease renewal failed', cause);
      abortController.abort(lostError);
    }
    cancelScheduled?.();
    cancelScheduled = null;
    return lostError;
  };

  const assertOwned = () => {
    if (lostError) throw lostError;
    if (stopped) throw new WebhookLeaseLostError('Stripe webhook lease heartbeat was stopped');
  };

  const renewNow = () => {
    assertOwned();
    if (!inFlightRenewal) {
      inFlightRenewal = (async () => {
        try {
          if (await renew() !== true) {
            throw new WebhookLeaseLostError('Stripe webhook lease is no longer owned');
          }
        } catch (error) {
          throw markLost(error);
        }
      })().finally(() => {
        inFlightRenewal = null;
      });
    }
    return inFlightRenewal;
  };

  const scheduleNext = () => {
    if (stopped || lostError) return;
    cancelScheduled = scheduleHeartbeat(() => {
      cancelScheduled = null;
      void renewNow().then(scheduleNext, () => {});
    }, heartbeatIntervalMs);
  };

  scheduleNext();

  return {
    publicLease: {
      signal: abortController.signal,
      assertOwned,
      // This is a database ownership fence, not just an in-memory assertion.
      // Call it immediately before every webhook-owned business-state write.
      fence: renewNow,
    },
    async renewAndStop() {
      await renewNow();
      stopped = true;
      cancelScheduled?.();
      cancelScheduled = null;
      if (inFlightRenewal) await inFlightRenewal;
      if (lostError) throw lostError;
    },
    async stop() {
      stopped = true;
      cancelScheduled?.();
      cancelScheduled = null;
      if (inFlightRenewal) await inFlightRenewal;
    },
  };
}

/**
 * Keep the lease lifecycle independent from Stripe event business logic so the
 * crash/retry behavior can be tested without accepting unsigned HTTP payloads.
 *
 * The abort signal is cooperative: it prevents new work after ownership loss,
 * but cannot retract a Stripe or database call that was already issued. Every
 * database business-state write must therefore await lease.fence() first.
 *
 * @param {{
 *   claim: () => Promise<string>,
 *   renew: () => Promise<boolean>,
 *   apply: (lease: {
 *     signal: AbortSignal,
 *     assertOwned: () => void,
 *     fence: () => Promise<void>,
 *   }) => Promise<void>,
 *   complete: () => Promise<boolean>,
 *   fail: (error: unknown) => Promise<unknown>,
 *   heartbeatIntervalMs: number,
 *   scheduleHeartbeat?: (callback: () => void, delayMs: number) => () => void,
 *   reportCleanupError?: (error: unknown) => void,
 * }} operations
 */
export async function processWebhookInbox({
  claim,
  renew,
  apply,
  complete,
  fail,
  heartbeatIntervalMs,
  scheduleHeartbeat = defaultScheduleHeartbeat,
  reportCleanupError = () => {},
}) {
  const outcome = await claim();
  if (!CLAIM_OUTCOMES.has(outcome)) throw new Error('Invalid webhook claim outcome');
  if (outcome === 'processed') return { kind: 'duplicate' };
  if (outcome === 'busy') return { kind: 'busy' };

  const heartbeat = startLeaseHeartbeat({
    renew,
    heartbeatIntervalMs,
    scheduleHeartbeat,
  });
  try {
    await apply(heartbeat.publicLease);
    // A final renewal closes the gap between the last application write and
    // acknowledgement. The SQL completion RPC independently checks ownership
    // and lease expiry as the authoritative fence.
    await heartbeat.renewAndStop();
    if (!await complete()) {
      throw new WebhookLeaseLostError('Stripe webhook lease was lost before completion');
    }
    return { kind: 'processed' };
  } catch (error) {
    try {
      await heartbeat.stop();
    } catch (heartbeatError) {
      reportCleanupError(heartbeatError);
    }
    try {
      // Failure cleanup is owner-and-expiry scoped. A stale invocation cannot
      // alter a row that another delivery reclaimed.
      await fail(error);
    } catch (cleanupError) {
      reportCleanupError(cleanupError);
    }
    throw error;
  }
}

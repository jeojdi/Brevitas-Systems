/**
 * Rate Limiting System for DDoS Protection and Database Overload Prevention
 *
 * Features:
 * - IP-based rate limiting with multiple tiers
 * - Sliding window algorithm for accurate rate limiting
 * - Automatic cleanup of expired entries
 * - DDoS detection and blocking
 * - Rate limit headers for transparency
 */

import { NextRequest } from 'next/server';

interface RateLimitConfig {
  windowMs: number;        // Time window in milliseconds
  maxRequests: number;      // Maximum requests per window
  blockDurationMs?: number; // How long to block after limit exceeded
  message?: string;         // Custom error message
}

interface RateLimitEntry {
  requests: number;
  windowStart: number;
  lastSeen: number;
  blocked: boolean;
  blockExpiry?: number;
}

interface RateLimiterOptions {
  maxEntries?: number;
  entryTtlMs?: number;
  cleanupIntervalMs?: number;
  now?: () => number;
}

function finiteInteger(value: unknown, fallback: number, minimum: number, maximum: number): number {
  const parsed = value === undefined ? fallback : Number(value);
  if (!Number.isFinite(parsed) || !Number.isInteger(parsed)) {
    throw new Error('Rate limiter resource bounds must be finite integers');
  }
  return Math.min(maximum, Math.max(minimum, parsed));
}

// Different rate limit tiers for different endpoints
export const RATE_LIMITS = {
  // Strict limit for form submissions (prevent spam)
  formSubmission: {
    windowMs: 60 * 1000,      // 1 minute
    maxRequests: 3,           // 3 submissions per minute
    blockDurationMs: 5 * 60 * 1000, // Block for 5 minutes after limit exceeded
    message: 'Too many form submissions. Please wait 5 minutes before trying again.'
  },

  // Medium limit for API endpoints
  api: {
    windowMs: 60 * 1000,      // 1 minute
    maxRequests: 30,          // 30 requests per minute
    blockDurationMs: 60 * 1000, // Block for 1 minute
    message: 'Too many API requests. Please slow down.'
  },

  // Lenient limit for general browsing
  general: {
    windowMs: 60 * 1000,      // 1 minute
    maxRequests: 100,         // 100 requests per minute
    blockDurationMs: 30 * 1000, // Block for 30 seconds
    message: 'Too many requests. Please wait a moment.'
  },

  // Aggressive DDoS protection
  ddos: {
    windowMs: 10 * 1000,      // 10 seconds
    maxRequests: 50,          // 50 requests per 10 seconds
    blockDurationMs: 30 * 60 * 1000, // Block for 30 minutes
    message: 'Suspicious activity detected. Access temporarily blocked.'
  }
};

export class RateLimiter {
  private store: Map<string, RateLimitEntry> = new Map();
  private cleanupInterval: NodeJS.Timeout;
  private readonly maxEntries: number;
  private readonly entryTtlMs: number;
  private readonly now: () => number;

  constructor(options: RateLimiterOptions = {}) {
    this.maxEntries = finiteInteger(
      options.maxEntries ?? process.env.BREVITAS_WEB_RATE_LIMIT_MAX_ENTRIES,
      50_000,
      1,
      250_000,
    );
    this.entryTtlMs = finiteInteger(
      options.entryTtlMs ?? process.env.BREVITAS_WEB_RATE_LIMIT_TTL_MS,
      60 * 60 * 1000,
      1000,
      24 * 60 * 60 * 1000,
    );
    this.now = options.now ?? Date.now;
    const intervalMs = finiteInteger(
      options.cleanupIntervalMs ?? process.env.BREVITAS_WEB_RATE_LIMIT_CLEANUP_MS,
      60 * 1000,
      1000,
      60 * 60 * 1000,
    );
    this.cleanupInterval = setInterval(() => {
      this.cleanup();
    }, intervalMs);
    this.cleanupInterval.unref?.();
  }

  /**
   * Get client IP address from request
   */
  private getClientIp(request: NextRequest): string {
    // Try different headers in order of reliability
    const forwarded = request.headers.get('x-forwarded-for');
    const real = request.headers.get('x-real-ip');
    const clientIp = request.headers.get('x-client-ip');

    if (forwarded) {
      // x-forwarded-for can contain multiple IPs, take the first one
      return forwarded.split(',')[0].trim();
    }

    return real || clientIp || 'unknown';
  }

  /**
   * Generate a unique key for rate limiting
   */
  private getKey(ip: string, endpoint: string): string {
    return `${ip}:${endpoint}`;
  }

  private keyIsBounded(key: string): boolean {
    return new TextEncoder().encode(key).byteLength <= 512;
  }

  /**
   * Check if a request should be rate limited
   */
  async checkLimit(
    request: NextRequest,
    endpoint: string,
    config: RateLimitConfig
  ): Promise<{
    allowed: boolean;
    remaining: number;
    reset: number;
    retryAfter?: number;
    message?: string;
  }> {
    const ip = this.getClientIp(request);
    const key = this.getKey(ip, endpoint);
    const now = this.now();
    const windowMs = finiteInteger(config.windowMs, 60_000, 1, 24 * 60 * 60 * 1000);
    const maxRequests = finiteInteger(config.maxRequests, 30, 1, 1_000_000);
    const blockDurationMs = finiteInteger(
      config.blockDurationMs,
      60_000,
      1,
      24 * 60 * 60 * 1000,
    );

    if (!this.keyIsBounded(key)) {
      return {
        allowed: false,
        remaining: 0,
        reset: now + 1000,
        retryAfter: 1,
        message: 'Rate limit identity is too large'
      };
    }

    let entry = this.store.get(key);

    // Check if IP is currently blocked
    if (entry?.blocked && entry.blockExpiry && entry.blockExpiry > now) {
      const retryAfter = Math.ceil((entry.blockExpiry - now) / 1000);
      return {
        allowed: false,
        remaining: 0,
        reset: entry.blockExpiry,
        retryAfter,
        message: config.message || 'Rate limit exceeded'
      };
    }

    // Initialize or reset entry if window expired
    if (!entry || now - entry.windowStart > windowMs) {
      this.cleanup();
      if (!entry && this.store.size >= this.maxEntries) {
        // Capacity pressure must not evict an active limiter entry and let a new
        // identity bypass enforcement. Deny until an expired slot is available.
        return {
          allowed: false,
          remaining: 0,
          reset: now + 1000,
          retryAfter: 1,
          message: 'Rate limiter capacity reached'
        };
      }
      entry = {
        requests: 0,
        windowStart: now,
        lastSeen: now,
        blocked: false
      };
      this.store.set(key, entry);
    }

    // Increment request count
    entry.lastSeen = now;
    entry.requests++;

    // Check if limit exceeded
    if (entry.requests > maxRequests) {
      // Block the IP
      entry.blocked = true;
      entry.blockExpiry = now + blockDurationMs;

      const retryAfter = Math.ceil(blockDurationMs / 1000);

      // Log potential DDoS attempt if excessive
      if (entry.requests > maxRequests * 3) {
        console.warn(`[SECURITY] Potential DDoS from IP ${ip}: ${entry.requests} requests in ${windowMs}ms`);
      }

      return {
        allowed: false,
        remaining: 0,
        reset: entry.blockExpiry,
        retryAfter,
        message: config.message || 'Rate limit exceeded'
      };
    }

    // Request allowed
    const remaining = maxRequests - entry.requests;
    const reset = entry.windowStart + windowMs;

    return {
      allowed: true,
      remaining,
      reset
    };
  }

  /**
   * Clean up expired entries to prevent memory leaks
   */
  private cleanup(): void {
    const now = this.now();
    const expired: string[] = [];

    this.store.forEach((entry, key) => {
      // A hard TTL applies even if future endpoint configurations have long windows.
      if (now - entry.lastSeen >= this.entryTtlMs) {
        expired.push(key);
      }
      else if (entry.blocked && entry.blockExpiry && now >= entry.blockExpiry) {
        expired.push(key);
      }
    });

    expired.forEach(key => this.store.delete(key));

    if (expired.length > 0) {
      console.log(`[RateLimiter] Cleaned up ${expired.length} expired entries`);
    }
  }

  /**
   * Get current stats for monitoring
   */
  getStats(): {
    totalEntries: number;
    blockedIps: number;
    topOffenders: Array<{ ip: string; requests: number }>;
  } {
    this.cleanup();
    const blockedIps = Array.from(this.store.entries()).filter(
      ([, entry]) => entry.blocked
    ).length;

    const topOffenders = Array.from(this.store.entries())
      .map(([key, entry]) => ({
        ip: key.split(':')[0],
        requests: entry.requests
      }))
      .sort((a, b) => b.requests - a.requests)
      .slice(0, 10);

    return {
      totalEntries: this.store.size,
      blockedIps,
      topOffenders
    };
  }

  /**
   * Manually block an IP address
   */
  blockIp(ip: string, durationMs: number = 3600000): void {
    const key = this.getKey(ip, 'manual-block');
    if (!this.keyIsBounded(key)) {
      throw new Error('Rate limit identity is too large');
    }
    const now = this.now();
    const duration = finiteInteger(durationMs, 3600000, 1000, this.entryTtlMs);
    this.cleanup();
    if (!this.store.has(key) && this.store.size >= this.maxEntries) {
      throw new Error('Rate limiter capacity reached; manual block was not retained');
    }
    this.store.set(key, {
      requests: 999999,
      windowStart: now,
      lastSeen: now,
      blocked: true,
      blockExpiry: now + duration
    });
    console.log(`[RateLimiter] Manually blocked IP ${ip} for ${duration}ms`);
  }

  /**
   * Manually unblock an IP address
   */
  unblockIp(ip: string): void {
    // Remove all entries for this IP
    const keysToDelete: string[] = [];
    this.store.forEach((_, key) => {
      if (key.startsWith(`${ip}:`)) {
        keysToDelete.push(key);
      }
    });
    keysToDelete.forEach(key => this.store.delete(key));
    console.log(`[RateLimiter] Unblocked IP ${ip}`);
  }

  /**
   * Cleanup on shutdown
   */
  destroy(): void {
    if (this.cleanupInterval) {
      clearInterval(this.cleanupInterval);
    }
  }
}

// Singleton instance
let rateLimiterInstance: RateLimiter | null = null;

export function getRateLimiter(): RateLimiter {
  if (!rateLimiterInstance) {
    rateLimiterInstance = new RateLimiter();
  }
  return rateLimiterInstance;
}

/**
 * Helper function to create rate limit headers
 */
export function createRateLimitHeaders(result: {
  remaining: number;
  reset: number;
  retryAfter?: number;
}): Record<string, string> {
  const headers: Record<string, string> = {
    'X-RateLimit-Remaining': result.remaining.toString(),
    'X-RateLimit-Reset': new Date(result.reset).toISOString(),
  };

  if (result.retryAfter !== undefined) {
    headers['Retry-After'] = result.retryAfter.toString();
  }

  return headers;
}

/**
 * Rate limit middleware for API routes
 */
export async function withRateLimit(
  request: NextRequest,
  handler: (request: NextRequest) => Promise<Response>,
  config: RateLimitConfig = RATE_LIMITS.api
): Promise<Response> {
  const rateLimiter = getRateLimiter();
  const endpoint = new URL(request.url).pathname;

  // Check both endpoint-specific and DDoS limits
  const [endpointCheck, ddosCheck] = await Promise.all([
    rateLimiter.checkLimit(request, endpoint, config),
    rateLimiter.checkLimit(request, 'ddos', RATE_LIMITS.ddos)
  ]);

  // Apply DDoS protection first
  if (!ddosCheck.allowed) {
    return new Response(
      JSON.stringify({
        error: 'DDoS protection triggered',
        message: ddosCheck.message,
        retryAfter: ddosCheck.retryAfter
      }),
      {
        status: 429,
        headers: {
          'Content-Type': 'application/json',
          ...createRateLimitHeaders(ddosCheck)
        }
      }
    );
  }

  // Then check endpoint-specific limits
  if (!endpointCheck.allowed) {
    return new Response(
      JSON.stringify({
        error: 'Rate limit exceeded',
        message: endpointCheck.message,
        retryAfter: endpointCheck.retryAfter
      }),
      {
        status: 429,
        headers: {
          'Content-Type': 'application/json',
          ...createRateLimitHeaders(endpointCheck)
        }
      }
    );
  }

  // Process the request and add rate limit headers to response
  const response = await handler(request);
  const newHeaders = new Headers(response.headers);
  Object.entries(createRateLimitHeaders(endpointCheck)).forEach(([key, value]) => {
    newHeaders.set(key, value);
  });

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: newHeaders
  });
}

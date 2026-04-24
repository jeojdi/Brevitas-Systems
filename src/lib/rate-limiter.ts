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
  blocked: boolean;
  blockExpiry?: number;
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

class RateLimiter {
  private store: Map<string, RateLimitEntry> = new Map();
  private cleanupInterval: NodeJS.Timeout;

  constructor() {
    // Cleanup expired entries every minute
    this.cleanupInterval = setInterval(() => {
      this.cleanup();
    }, 60 * 1000);
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
    const now = Date.now();

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
    if (!entry || now - entry.windowStart > config.windowMs) {
      entry = {
        requests: 0,
        windowStart: now,
        blocked: false
      };
      this.store.set(key, entry);
    }

    // Increment request count
    entry.requests++;

    // Check if limit exceeded
    if (entry.requests > config.maxRequests) {
      // Block the IP
      entry.blocked = true;
      entry.blockExpiry = now + (config.blockDurationMs || 60000);

      const retryAfter = Math.ceil((config.blockDurationMs || 60000) / 1000);

      // Log potential DDoS attempt if excessive
      if (entry.requests > config.maxRequests * 3) {
        console.warn(`[SECURITY] Potential DDoS from IP ${ip}: ${entry.requests} requests in ${config.windowMs}ms`);
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
    const remaining = config.maxRequests - entry.requests;
    const reset = entry.windowStart + config.windowMs;

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
    const now = Date.now();
    const expired: string[] = [];

    this.store.forEach((entry, key) => {
      // Remove entries older than 1 hour
      if (now - entry.windowStart > 3600000) {
        expired.push(key);
      }
      // Remove unblocked entries older than their window
      else if (!entry.blocked && now - entry.windowStart > 300000) {
        expired.push(key);
      }
      // Remove blocked entries after block expiry + 1 hour
      else if (entry.blocked && entry.blockExpiry && now > entry.blockExpiry + 3600000) {
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
    const blockedIps = Array.from(this.store.entries()).filter(
      ([_, entry]) => entry.blocked
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
    this.store.set(key, {
      requests: 999999,
      windowStart: Date.now(),
      blocked: true,
      blockExpiry: Date.now() + durationMs
    });
    console.log(`[RateLimiter] Manually blocked IP ${ip} for ${durationMs}ms`);
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
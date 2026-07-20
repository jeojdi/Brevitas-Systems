import 'server-only';

const ADMIN_REQUEST_MAX_BYTES = 64 * 1024;
const ADMIN_RESPONSE_MAX_BYTES = 1024 * 1024;
const ADMIN_TIMEOUT_MS = 8_000;
const SEGMENT = /^[A-Za-z0-9_-]{1,100}$/;
const REQUEST_ID = /^[A-Za-z0-9._:-]{8,128}$/;

function backendOrigin(): URL {
  const configured = process.env.BREVITAS_API_URL?.trim();
  if (!configured) throw new Error('BREVITAS_API_URL is not configured');
  const origin = new URL(configured);
  if (origin.protocol !== 'https:' && process.env.NODE_ENV === 'production') {
    throw new Error('BREVITAS_API_URL must use HTTPS in production');
  }
  return origin;
}

function upstreamPath(path: string[]): string {
  if (!path.length || path[0] !== 'company' || path.some((part) => !SEGMENT.test(part))) {
    throw new TypeError('Unsupported administration path');
  }
  return `/v1/${path.map(encodeURIComponent).join('/')}`;
}

function bearer(request: Request): string {
  const value = request.headers.get('authorization') || '';
  if (!/^Bearer [A-Za-z0-9._~+\/-]{20,8192}$/.test(value)) {
    throw new RangeError('Authentication required');
  }
  return value;
}

async function boundedBody(request: Request): Promise<ArrayBuffer | undefined> {
  if (request.method === 'GET' || request.method === 'HEAD') return undefined;
  const declared = Number(request.headers.get('content-length') || 0);
  if (declared > ADMIN_REQUEST_MAX_BYTES) throw new RangeError('Request body too large');
  const body = await request.arrayBuffer();
  if (body.byteLength > ADMIN_REQUEST_MAX_BYTES) throw new RangeError('Request body too large');
  return body;
}

function secureJson(body: Record<string, string>, status: number, extra: HeadersInit = {}): Response {
  return Response.json(body, {
    status,
    headers: {
      'Cache-Control': 'private, no-store',
      'X-Content-Type-Options': 'nosniff',
      ...Object.fromEntries(new Headers(extra)),
    },
  });
}

async function boundedResponseBody(response: Response): Promise<ArrayBuffer | null> {
  const lengthValue = response.headers.get('content-length');
  if (lengthValue !== null) {
    const declared = Number(lengthValue);
    if (!Number.isSafeInteger(declared) || declared < 0 || declared > ADMIN_RESPONSE_MAX_BYTES) {
      await response.body?.cancel();
      return null;
    }
  }
  if (!response.body) return new ArrayBuffer(0);
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > ADMIN_RESPONSE_MAX_BYTES) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body.buffer as ArrayBuffer;
}

export async function proxyCompanyAdmin(request: Request, path: string[]): Promise<Response> {
  let origin: URL;
  try {
    origin = backendOrigin();
  } catch {
    return secureJson({ error: 'Company administration unavailable' }, 503);
  }
  let pathname: string;
  try {
    pathname = upstreamPath(path);
  } catch {
    return secureJson({ error: 'Not found' }, 404);
  }
  let authorization: string;
  try {
    authorization = bearer(request);
  } catch {
    return secureJson({ error: 'Authentication required' }, 401);
  }

  let body: ArrayBuffer | undefined;
  try {
    body = await boundedBody(request);
  } catch {
    return secureJson({ error: 'Request body too large' }, 413);
  }

  const destination = new URL(pathname, origin);
  destination.search = new URL(request.url).search;
  const incomingRequestId = request.headers.get('x-request-id') || '';
  const requestId = REQUEST_ID.test(incomingRequestId) ? incomingRequestId : crypto.randomUUID();
  const headers = new Headers({
    Accept: 'application/json',
    Authorization: authorization,
    'X-Request-ID': requestId,
  });
  if (body !== undefined) headers.set('Content-Type', 'application/json');

  try {
    const upstream = await fetch(destination, {
      method: request.method,
      headers,
      body,
      cache: 'no-store',
      signal: AbortSignal.timeout(ADMIN_TIMEOUT_MS),
    });
    const responseBody = await boundedResponseBody(upstream);
    if (responseBody === null) {
      return secureJson({ error: 'Upstream response exceeded limit' }, 502);
    }
    return new Response(responseBody, {
      status: upstream.status,
      headers: {
        'Cache-Control': 'private, no-store',
        'Content-Type': upstream.headers.get('content-type') || 'application/json',
        'X-Content-Type-Options': 'nosniff',
        'X-Request-ID': requestId,
      },
    });
  } catch {
    return secureJson(
      { error: 'Company administration unavailable' }, 503, { 'Retry-After': '1' });
  }
}

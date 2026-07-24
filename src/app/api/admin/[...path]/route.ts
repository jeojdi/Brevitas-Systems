import { proxyCompanyAdmin } from '@/lib/admin/proxy';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';
export const maxDuration = 10;

type Context = { params: Promise<{ path: string[] }> };

async function handler(request: Request, context: Context): Promise<Response> {
  const { path } = await context.params;
  return proxyCompanyAdmin(request, path);
}

export const GET = handler;
export const POST = handler;
export const PATCH = handler;
export const DELETE = handler;

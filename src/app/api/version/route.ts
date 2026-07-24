import { buildIdentity, productionBuildIdentityRequired } from "@/lib/build-provenance";

// Bake provenance into the deployment. A production Vercel build fails when its automatic
// immutable Git identity is absent, malformed, or conflicts with an explicitly supplied SHA.
export const dynamic = "force-static";

export async function GET() {
  return Response.json(
    { service: "dashboard", build: buildIdentity(productionBuildIdentityRequired()) },
    { headers: { "Cache-Control": "no-store" } },
  );
}

const fullCommitSha = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const releaseVersion = /^(?=.{1,64}$)v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
const imageDigest = /^sha256:[0-9a-f]{64}$/;
const rfc3339Timestamp = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

export type BuildIdentity = {
  commit_sha?: string;
  built_at?: string;
  version?: string;
  image_digest?: string;
};

function supplied(values: Array<string | undefined>): string[] {
  return values.map((value) => (value || "").trim()).filter(Boolean);
}

export function buildIdentity(required = false): BuildIdentity {
  const commits = supplied([
    process.env.BREVITAS_BUILD_SHA,
    process.env.VERCEL_GIT_COMMIT_SHA,
    process.env.RAILWAY_GIT_COMMIT_SHA,
    process.env.GITHUB_SHA,
  ]).map((value) => value.toLowerCase());
  if (commits.some((value) => !fullCommitSha.test(value))) {
    throw new Error("Build commit identity must be a full immutable Git SHA");
  }
  const distinctCommits = [...new Set(commits)];
  if (distinctCommits.length > 1) {
    throw new Error("Conflicting build commit identities were supplied");
  }
  if (required && distinctCommits.length === 0) {
    throw new Error("Production requires a full immutable build commit SHA");
  }

  const identity: BuildIdentity = {};
  if (distinctCommits[0]) identity.commit_sha = distinctCommits[0];

  const timestamp = (process.env.BREVITAS_BUILD_TIMESTAMP || "").trim();
  if (timestamp) {
    if (!rfc3339Timestamp.test(timestamp) || Number.isNaN(Date.parse(timestamp))) {
      throw new Error("Build timestamp must be an RFC 3339 timestamp");
    }
    identity.built_at = new Date(timestamp).toISOString();
  }

  const version = (process.env.BREVITAS_BUILD_VERSION || "").trim();
  if (version) {
    if (!releaseVersion.test(version)) throw new Error("Build version is invalid");
    identity.version = version;
  }

  const digest = (process.env.BREVITAS_IMAGE_DIGEST || "").trim().toLowerCase();
  if (digest) {
    if (!imageDigest.test(digest)) {
      throw new Error("Build image digest must be an immutable sha256 digest");
    }
    identity.image_digest = digest;
  }
  return identity;
}

export function productionBuildIdentityRequired(): boolean {
  return process.env.VERCEL_ENV === "production" ||
    ["1", "true", "yes"].includes(
      (process.env.BREVITAS_REQUIRE_BUILD_SHA || "").trim().toLowerCase(),
    );
}

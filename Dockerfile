# FastAPI backend (api/) for Railway.
# Deterministic build that ignores the Node/Next.js side of the repo entirely.
# Build context is the repo root so token_efficiency_model/ is importable
# (api/server.py inserts the repo root onto sys.path).
FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

# CI supplies BREVITAS_BUILD_SHA explicitly. Railway also makes its immutable Git SHA
# available as RAILWAY_GIT_COMMIT_SHA; the application rejects disagreement between them.
ARG BREVITAS_BUILD_SHA=""
ARG RAILWAY_GIT_COMMIT_SHA=""
ARG BREVITAS_BUILD_TIMESTAMP=""
ARG BREVITAS_BUILD_VERSION=""
LABEL org.opencontainers.image.revision="${BREVITAS_BUILD_SHA}"
LABEL org.opencontainers.image.created="${BREVITAS_BUILD_TIMESTAMP}"
LABEL org.opencontainers.image.version="${BREVITAS_BUILD_VERSION}"
ENV BREVITAS_BUILD_SHA="${BREVITAS_BUILD_SHA}" \
    RAILWAY_GIT_COMMIT_SHA="${RAILWAY_GIT_COMMIT_SHA}" \
    BREVITAS_BUILD_TIMESTAMP="${BREVITAS_BUILD_TIMESTAMP}" \
    BREVITAS_BUILD_VERSION="${BREVITAS_BUILD_VERSION}"

WORKDIR /app

# Install Python deps first for layer caching. All deps ship manylinux wheels
# (numpy, cryptography, pydantic-core, etc.), so no compiler is needed.
COPY scripts/ci/python-runtime.lock scripts/ci/python-runtime.lock
RUN python -m pip install --no-cache-dir --require-hashes \
    -r scripts/ci/python-runtime.lock

# App code + the token-efficiency engine it imports.
COPY api/ api/
COPY brevitas/ brevitas/
COPY token_efficiency_model/ token_efficiency_model/

RUN useradd --create-home --uid 10001 brevitas \
 && chown -R brevitas:brevitas /app
USER brevitas

# Railway injects $PORT at runtime; default to 8000 for local `docker run`.
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["sh", "-c", "exec uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-graceful-shutdown ${BREVITAS_SHUTDOWN_GRACE_SECONDS:-120}"]

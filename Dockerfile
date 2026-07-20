# FastAPI backend (api/) for Railway.
# Deterministic build that ignores the Node/Next.js side of the repo entirely.
# Build context is the repo root so token_efficiency_model/ is importable
# (api/server.py inserts the repo root onto sys.path).
FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

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

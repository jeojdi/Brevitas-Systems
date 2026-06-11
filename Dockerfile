# FastAPI backend (api/) for Railway.
# Deterministic build that ignores the Node/Next.js side of the repo entirely.
# Build context is the repo root so token_efficiency_model/ is importable
# (api/server.py inserts the repo root onto sys.path).
FROM python:3.11-slim

WORKDIR /app

# Install Python deps first for layer caching. All deps ship manylinux wheels
# (numpy, cryptography, pydantic-core, etc.), so no compiler is needed.
COPY api/requirements.txt api/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r api/requirements.txt

# App code + the token-efficiency engine it imports.
COPY api/ api/
COPY token_efficiency_model/ token_efficiency_model/

# Railway injects $PORT at runtime; default to 8000 for local `docker run`.
EXPOSE 8000
CMD ["sh", "-c", "uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8000}"]

FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

# Set working directory
WORKDIR /app

# Build context is the REPO ROOT (the default for Render/Fly/Railway and for
# `docker build -f services/compress/Dockerfile .`), so reference files from there.
COPY scripts/ci/python-compressor.lock scripts/ci/python-compressor.lock

# Install Python dependencies
# Note: torch CPU is large (~2GB); the full image will be ~3-4GB.
RUN python -m pip install --no-cache-dir --require-hashes \
    -r scripts/ci/python-compressor.lock

# Copy application code
COPY services/compress/app.py .
# Copy the self-contained telemetry module and only its dependency-free redaction module.
# Omitting brevitas/__init__.py keeps the compressor image independent from the
# SDK/token-engine and KMS/cryptography dependency graphs.
COPY brevitas/observability.py brevitas/observability.py
COPY brevitas/security/redaction.py brevitas/security/redaction.py

# The model cache and runtime never need root privileges.
RUN useradd --create-home --uid 10001 brevitas \
    && chown -R brevitas:brevitas /app /home/brevitas
USER brevitas

# Expose port
EXPOSE 8080
STOPSIGNAL SIGTERM

# Health check — uses stdlib urllib so no extra dependency is required.
# Long start-period: the model loads on first startup (can take 30-120s).
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/live',timeout=5).status==200 else 1)" || exit 1

# Run the application
CMD ["uvicorn", "app:app", "--host", "::", "--port", "8080", "--workers", "1", "--timeout-graceful-shutdown", "180"]

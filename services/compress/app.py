"""
LLMLingua-2 compression microservice.

Provides lossy and lossless prompt compression via FastAPI.
- POST /v1/optimize: compress a prompt
- GET /health: health check
"""

import asyncio
import os
import re
import secrets
import time
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import MutableHeaders
import tiktoken

from brevitas.observability import (
    REQUEST_ID_COMPAT_HEADER,
    REQUEST_ID_HEADER,
    StructuredLogger,
    configure_json_logging,
    correlation_context,
    get_runtime,
    normalize_request_id,
    route_label,
    shutdown_observability,
)


configure_json_logging(service="compressor", logger_names=("brevitas.compressor",))
logger = StructuredLogger("brevitas.compressor")

# Global state for model (loaded once at startup)
_LLMLINGUA = None
_MODEL_LOADED = False
_MODEL_LOAD_COMPLETE = False
_ACCEPTING_TRAFFIC = False
_MAX_PROMPT_CHARS = max(1, int(os.environ.get("BREVITAS_COMPRESS_MAX_PROMPT_CHARS", "1000000")))
_MAX_FORCE_TOKENS = max(1, int(os.environ.get("BREVITAS_COMPRESS_MAX_FORCE_TOKENS", "128")))
_MAX_CONCURRENCY = max(1, int(os.environ.get("BREVITAS_COMPRESS_CONCURRENCY", "2")))
_ADMISSION_TIMEOUT_S = max(0.01, float(os.environ.get("BREVITAS_COMPRESS_ADMISSION_TIMEOUT", "0.25")))
_INFERENCE_TIMEOUT_S = max(1.0, float(os.environ.get("BREVITAS_COMPRESS_INFERENCE_TIMEOUT", "120")))
_MAX_BODY_BYTES = _MAX_PROMPT_CHARS * 4 + 65536
_inference_slots = asyncio.Semaphore(_MAX_CONCURRENCY)


def normalize_prompt(text: str) -> str:
    """Lossless whitespace/format normalization.

    Code fences are left byte-identical so indentation-significant content
    (Python, YAML, etc.) is never altered.
    """
    if not text:
        return text

    fence_pattern = re.compile(r"(```.*?```|~~~.*?~~~)", re.DOTALL)
    parts = fence_pattern.split(text)
    out = []
    for i, seg in enumerate(parts):
        if i % 2 == 1:  # odd indices are the captured code fences
            out.append(seg)
            continue
        # prose segment: safe, meaning-preserving cleanups
        seg = re.sub(r"[ \t]+", " ", seg)        # runs of spaces/tabs -> single space
        seg = re.sub(r" *\n", "\n", seg)         # trailing spaces before newlines
        seg = re.sub(r"\n{3,}", "\n\n", seg)     # 3+ blank lines -> one blank line
        out.append(seg)
    return "".join(out).strip()


def count_tokens(text: str) -> int:
    """Count tokens using tiktoken cl100k_base."""
    try:
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text or "", disallowed_special=()))
    except Exception:
        # Fallback: rough estimate
        return max(1, int(len((text or "").split()) * 1.3))


def load_model():
    """Load LLMLingua-2 model at startup."""
    global _LLMLINGUA, _MODEL_LOADED, _MODEL_LOAD_COMPLETE
    _MODEL_LOAD_COMPLETE = False
    try:
        from llmlingua import PromptCompressor
        # PromptCompressor defaults device_map to "cuda"; detect so CPU-only hosts (most
        # containers, Apple Silicon) don't crash with "Torch not compiled with CUDA enabled".
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
        logger.info("compressor_model_loading", operation="compress")
        _LLMLINGUA = PromptCompressor(
            model_name="microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank",
            use_llmlingua2=True,
            device_map=device,
        )
        _MODEL_LOADED = True
        logger.info("compressor_model_loaded", operation="compress", outcome="success")
    except ImportError:
        logger.warning("compressor_dependency_unavailable", dependency="compressor",
                       outcome="unavailable")
        _LLMLINGUA = None
        _MODEL_LOADED = False
    except Exception as exc:
        logger.error("compressor_model_load_failed", operation="compress",
                     outcome="failed", error_type=type(exc).__name__)
        _LLMLINGUA = None
        _MODEL_LOADED = False
    finally:
        _MODEL_LOAD_COMPLETE = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load model on startup, cleanup on shutdown."""
    global _ACCEPTING_TRAFFIC, _LLMLINGUA, _MODEL_LOADED
    _ACCEPTING_TRAFFIC = False
    if not os.environ.get("BREVITAS_COMPRESS_TOKEN", "").strip():
        raise RuntimeError("BREVITAS_COMPRESS_TOKEN is required")
    await asyncio.to_thread(load_model)
    _ACCEPTING_TRAFFIC = _MODEL_LOADED
    get_runtime(default_service="compressor").metrics.record_service_operation(
        service="compressor", outcome="success" if _MODEL_LOADED else "unavailable")
    try:
        yield
    finally:
        # Uvicorn drains active requests before lifespan shutdown. Mark the replica unready
        # before releasing the large model so a rolling deploy cannot route new work here.
        _ACCEPTING_TRAFFIC = False
        _LLMLINGUA = None
        _MODEL_LOADED = False
        shutdown_observability()


app = FastAPI(title="LLMLingua Compression Service", lifespan=lifespan)


class CompressorObservabilityMiddleware:
    """Content-free request correlation and telemetry for the private service."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.application(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        request_id = normalize_request_id(
            headers.get(REQUEST_ID_HEADER.lower())
            or headers.get(REQUEST_ID_COMPAT_HEADER.lower())
            or ""
        )
        scope.setdefault("state", {})["brevitas_request_id"] = request_id
        runtime = get_runtime(default_service="compressor")
        started = time.perf_counter()
        status_code = 500

        async def correlated_send(message):
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
                outgoing = MutableHeaders(scope=message)
                outgoing[REQUEST_ID_HEADER] = request_id
                outgoing[REQUEST_ID_COMPAT_HEADER] = request_id
            await send(message)

        with correlation_context(request_id=request_id):
            with runtime.span("http.server.request", {
                "http.request.method": str(scope.get("method") or ""),
            }):
                try:
                    await self.application(scope, receive, correlated_send)
                finally:
                    route = route_label(
                        getattr(scope.get("route"), "path", ""), registered=True)
                    duration = time.perf_counter() - started
                    runtime.metrics.record_api_request(
                        duration_seconds=duration,
                        method=str(scope.get("method") or ""),
                        route=route,
                        status_code=status_code,
                        fault="brevitas",
                    )
                    runtime.metrics.record_service_operation(
                        service="compressor",
                        outcome="server_error" if status_code >= 500 else "success",
                    )
                    logger.info(
                        "compressor_request_completed",
                        method=str(scope.get("method") or ""), route=route,
                        status_code=status_code, duration_ms=duration * 1000,
                        outcome="server_error" if status_code >= 500 else "success",
                    )


@app.middleware("http")
async def reject_oversized_requests(request: Request, call_next):
    length = request.headers.get("content-length")
    if length:
        try:
            parsed_length = int(length)
            if parsed_length < 0:
                raise ValueError
            if parsed_length > _MAX_BODY_BYTES:
                return JSONResponse({"detail": "Request body too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
    # Bound the aggregate chunked/HTTP2 body even when an attacker supplies a
    # smaller Content-Length than the bytes actually sent.
    buffered = bytearray()
    async for chunk in request.stream():
        buffered.extend(chunk)
        if len(buffered) > _MAX_BODY_BYTES:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": bytes(buffered), "more_body": False}

    request._receive = receive
    return await call_next(request)


app.add_middleware(CompressorObservabilityMiddleware)


class OptimizeRequest(BaseModel):
    """Request body for /v1/optimize endpoint."""
    prompt: str = Field(min_length=1, max_length=_MAX_PROMPT_CHARS)
    rate: float = Field(default=0.5, ge=0.05, le=1.0)
    force_tokens: Optional[list[str]] = Field(default=None, max_length=_MAX_FORCE_TOKENS)


class OptimizeResponse(BaseModel):
    """Response from /v1/optimize endpoint."""
    compressed_prompt: str
    tokens_before: int
    tokens_after: int
    saved_pct: float
    method: str  # "lossless" or "llmlingua2+lossless"
    lossy: bool


class HealthResponse(BaseModel):
    """Response from /health endpoint."""
    status: str
    model_loaded: bool


def verify_token(authorization: Optional[str] = Header(None)) -> bool:
    """Require the private service-to-service bearer token."""
    required_token = os.environ.get("BREVITAS_COMPRESS_TOKEN", "").strip()
    if not required_token:
        raise HTTPException(status_code=503, detail="Compression service is not configured")

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    if not secrets.compare_digest(parts[1], required_token):
        raise HTTPException(status_code=403, detail="Invalid token")

    return True


@app.get("/live")
async def liveness():
    return {"status": "ok"}


@app.get("/startup")
async def startup_check():
    payload = {"status": "ok" if _MODEL_LOAD_COMPLETE else "starting"}
    if not _MODEL_LOAD_COMPLETE:
        return JSONResponse(payload, status_code=503)
    return payload


@app.get("/health", response_model=HealthResponse)
@app.get("/ready", response_model=HealthResponse)
async def health_check():
    """Readiness check: lossy compression must not receive traffic without its model."""
    payload = HealthResponse(
        status="ok" if _MODEL_LOADED and _ACCEPTING_TRAFFIC else "unavailable",
        model_loaded=_MODEL_LOADED,
    )
    if not _MODEL_LOADED or not _ACCEPTING_TRAFFIC:
        return JSONResponse(payload.model_dump(), status_code=503)
    return payload


def _optimize_sync(request: OptimizeRequest) -> OptimizeResponse:
    tokens_before = count_tokens(request.prompt)

    # "Lossless" must be byte-identical. Whitespace normalization can corrupt
    # indentation-sensitive Python, YAML, Makefiles, and Markdown.
    if request.rate >= 1.0:
        return OptimizeResponse(
            compressed_prompt=request.prompt,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            saved_pct=0.0,
            method="lossless",
            lossy=False,
        )
    if _LLMLINGUA is None:
        return OptimizeResponse(
            compressed_prompt=request.prompt,
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            saved_pct=0.0,
            method="lossless",
            lossy=False,
        )

    force_tokens = request.force_tokens or ["\n", ".", "!", "?", ",", ":"]
    if any(not token or len(token) > 128 for token in force_tokens):
        raise ValueError("invalid force token")
    normalized = normalize_prompt(request.prompt)
    try:
        result = _LLMLINGUA.compress_prompt(
            normalized,
            rate=request.rate,
            force_tokens=force_tokens,
        )
    except Exception as exc:
        logger.error("compressor_model_inference_failed", operation="compress",
                     outcome="failed", error_type=type(exc).__name__)
        raise RuntimeError("compression model inference failed") from exc
    compressed = result.get("compressed_prompt", normalized)
    tokens_after = count_tokens(compressed)
    return OptimizeResponse(
        compressed_prompt=compressed,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        saved_pct=round(100 * (1 - tokens_after / max(1, tokens_before)), 2),
        method="llmlingua2+lossless",
        lossy=True,
    )


@app.post("/v1/optimize", response_model=OptimizeResponse)
async def optimize_prompt(
    request: OptimizeRequest,
    _: bool = Depends(verify_token)
):
    """Compress a prompt using LLMLingua-2 or lossless normalization."""
    try:
        await asyncio.wait_for(_inference_slots.acquire(), timeout=_ADMISSION_TIMEOUT_S)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=429,
            detail="Compression service is at capacity",
            headers={"Retry-After": "1"},
        ) from exc

    release_in_finally = True
    try:
        runtime = get_runtime(default_service="compressor")
        with runtime.span("compressor.inference", {
            "brevitas.operation": "compress", "brevitas.dependency": "compressor",
        }):
            task = asyncio.create_task(asyncio.to_thread(_optimize_sync, request))
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task), timeout=_INFERENCE_TIMEOUT_S)
            except TimeoutError as exc:
                release_in_finally = False

                def release_when_finished(done: asyncio.Task) -> None:
                    try:
                        done.exception()
                    except BaseException:
                        pass
                    _inference_slots.release()

                task.add_done_callback(release_when_finished)
                raise HTTPException(status_code=504, detail="Compression timed out") from exc
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid compression parameters") from exc
    except Exception as exc:
        logger.error("compressor_inference_failed", operation="compress",
                     outcome="failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=503, detail="Compression temporarily unavailable") from exc
    finally:
        if release_in_finally:
            _inference_slots.release()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

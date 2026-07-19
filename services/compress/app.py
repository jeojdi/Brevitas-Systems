"""
LLMLingua-2 compression microservice.

Provides lossy and lossless prompt compression via FastAPI.
- POST /v1/optimize: compress a prompt
- GET /health: health check
"""

import os
import re
import logging
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
import tiktoken

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global state for model (loaded once at startup)
_LLMLINGUA = None
_MODEL_LOADED = False


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
    global _LLMLINGUA, _MODEL_LOADED
    try:
        from llmlingua import PromptCompressor
        # PromptCompressor defaults device_map to "cuda"; detect so CPU-only hosts (most
        # containers, Apple Silicon) don't crash with "Torch not compiled with CUDA enabled".
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            device = "cpu"
        logger.info("Loading LLMLingua-2 model on %s...", device)
        _LLMLINGUA = PromptCompressor(
            model_name="microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank",
            use_llmlingua2=True,
            device_map=device,
        )
        _MODEL_LOADED = True
        logger.info("LLMLingua-2 model loaded successfully")
    except ImportError:
        logger.warning("llmlingua package not installed; compression will fall back to lossless")
        _LLMLINGUA = None
        _MODEL_LOADED = False
    except Exception as e:
        logger.error(f"Failed to load LLMLingua-2 model: {e}")
        _LLMLINGUA = None
        _MODEL_LOADED = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load model on startup, cleanup on shutdown."""
    load_model()
    yield
    # Cleanup if needed


app = FastAPI(title="LLMLingua Compression Service", lifespan=lifespan)


class OptimizeRequest(BaseModel):
    """Request body for /v1/optimize endpoint."""
    prompt: str
    rate: float = 0.5  # target keep ratio; 1.0 = lossless only
    force_tokens: Optional[list] = None


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
    """Verify Bearer token if BREVITAS_COMPRESS_TOKEN is set."""
    required_token = os.environ.get("BREVITAS_COMPRESS_TOKEN")
    if not required_token:
        return True  # No auth required

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    if parts[1] != required_token:
        raise HTTPException(status_code=403, detail="Invalid token")

    return True


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint."""
    return HealthResponse(
        status="ok",
        model_loaded=_MODEL_LOADED
    )


@app.post("/v1/optimize", response_model=OptimizeResponse)
async def optimize_prompt(
    request: OptimizeRequest,
    _: bool = Depends(verify_token)
):
    """Compress a prompt using LLMLingua-2 or lossless normalization."""
    if not request.prompt:
        raise HTTPException(status_code=400, detail="prompt cannot be empty")

    try:
        tokens_before = count_tokens(request.prompt)

        # Lossless MUST be byte-identical. The old path ran normalize_prompt here, which
        # collapsed whitespace outside code fences and corrupted indentation-significant
        # content (YAML, Python, Makefiles, Markdown). Lossless now returns the input
        # verbatim; whitespace normalization is only ever a lossy-path pre-step.
        if request.rate >= 1.0 or _LLMLINGUA is None:
            return OptimizeResponse(
                compressed_prompt=request.prompt,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                saved_pct=0.0,
                method="lossless",
                lossy=False
            )

        # Lossy path: normalization is allowed here because the caller opted into a
        # lossy rewrite (rate < 1.0) and LLMLingua-2 is available.
        normalized = normalize_prompt(request.prompt)

        # Try lossy compression with LLMLingua-2
        try:
            force_tokens = request.force_tokens or ["\n", ".", "!", "?", ",", ":"]
            result = _LLMLINGUA.compress_prompt(
                normalized,
                rate=request.rate,
                force_tokens=force_tokens
            )
            compressed = result.get("compressed_prompt", normalized)
        except Exception as e:
            logger.error(f"LLMLingua-2 compression failed: {e}")
            # Fall back to lossless — return the ORIGINAL bytes, never the normalized form.
            tokens_after = tokens_before
            return OptimizeResponse(
                compressed_prompt=request.prompt,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                saved_pct=0.0,
                method="lossless",
                lossy=False
            )

        # Lossy compression succeeded
        tokens_after = count_tokens(compressed)
        saved_pct = round(100 * (1 - tokens_after / max(1, tokens_before)), 2)
        return OptimizeResponse(
            compressed_prompt=compressed,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            saved_pct=saved_pct,
            method="llmlingua2+lossless",
            lossy=True
        )

    except Exception as e:
        logger.error(f"Compression error: {e}")
        raise HTTPException(status_code=500, detail=f"Compression failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

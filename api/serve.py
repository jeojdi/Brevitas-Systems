"""Dual-stack launcher for the FastAPI backend.

Railway's healthcheck prober connects over IPv4 while the private mesh
(api.railway.internal, which the worker and compressor resolve) is IPv6, and
uvicorn's CLI cannot dual-bind: `--host 0.0.0.0` leaves the IPv6 mesh with
NO_SOCKET, and `--host ::` is IPv6-only because CPython's asyncio sets
IPV6_V6ONLY=1 on every AF_INET6 socket it creates, so it failed the IPv4-side
/v1/health/ready check on every deploy. Bind the socket ourselves with
IPV6_V6ONLY=0 so one listener serves both families. Mirrors
services/compress/serve.py.
"""
import os
import socket

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    uvicorn.run(
        "api.server:app",
        fd=sock.fileno(),
        workers=1,
        # Names the trusted edge proxy hop(s) so request.client.host reflects the real
        # client peer (which the rate limiter buckets on) instead of collapsing every
        # request onto the edge IP. _validate_runtime_config requires it in production;
        # default to loopback for local `docker run`.
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        timeout_graceful_shutdown=int(
            os.environ.get("BREVITAS_SHUTDOWN_GRACE_SECONDS", "120")),
        # ASGI-layer admission ceiling. Every JSON POST is buffered and parsed by
        # _AggregateRequestBoundsMiddleware before the per-tenant concurrency lease
        # is taken, so the limits in api/distributed_limits.py bound nothing about
        # peak memory: with no ceiling here, resident size is bounded only by how
        # many connections the kernel hands us, and an OOM kill also strands every
        # limiter lease in Redis. Anchored to organization_concurrency=200 so one
        # organization at its full policy ceiling can still saturate a replica and
        # nothing beyond that is admitted; over it uvicorn answers 503 without
        # reading a body. Worst-case body memory is roughly limit_concurrency x
        # BREVITAS_REQUEST_MAX_BYTES x 5 (the middleware retains the raw messages,
        # a byte copy and a parsed graph; the proxy handler re-buffers and
        # re-parses), so lower this on a container smaller than the 2 GB in
        # deploy/, or after raising the request-size ceiling.
        limit_concurrency=int(os.environ.get("BREVITAS_MAX_CONCURRENCY", "200")),
        backlog=int(os.environ.get("BREVITAS_LISTEN_BACKLOG", "256")),
    )


if __name__ == "__main__":
    main()

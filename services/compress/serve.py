"""Dual-stack launcher for the compressor.

Railway's healthcheck prober connects over IPv4 while the private mesh
(compressor.railway.internal) is IPv6, and uvicorn's CLI cannot dual-bind:
`--host 0.0.0.0` breaks mesh reachability, `--host ::` inherits the kernel's
IPV6_V6ONLY default and has repeatedly failed the IPv4-side healthcheck on
Railway's V2 runtime. Bind the socket ourselves with IPV6_V6ONLY=0 so one
listener serves both families regardless of kernel defaults.
"""
import os
import socket

import uvicorn


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    uvicorn.run(
        "app:app",
        fd=sock.fileno(),
        workers=1,
        timeout_graceful_shutdown=int(
            os.environ.get("BREVITAS_SHUTDOWN_GRACE_SECONDS", "180")),
    )


if __name__ == "__main__":
    main()

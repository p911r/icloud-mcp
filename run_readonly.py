#!/usr/bin/env python3
"""
Read-only HTTP entrypoint for the iCloud MCP fork.

mike-tih/icloud-mcp ships with NO transport authentication and exposes /mcp openly.
On a public Railway URL holding your iCloud app-specific password that is
unacceptable, so this entrypoint adds a gate. Two modes, auto-selected by env:

  MODE A - URL-path secret (default-friendly; works when the client can only
           store a plain URL, e.g. the Cowork "add custom connector" dialog with
           no header field):
             set MCP_URL_SECRET=<long random>
             MCP endpoint served at  /<secret>/mcp
             The unguessable path IS the gate. Over TLS this is fine for a
             read-only, low-sensitivity inbox.

  MODE B - Bearer header (use when the client CAN send Authorization headers):
             set MCP_BEARER_TOKEN=<long random>  (and DON'T set MCP_URL_SECRET)
             MCP endpoint at /mcp; requires  Authorization: Bearer <token>

  /health is always open (Railway/Docker health check).

iCloud creds come from env (Railway secrets): ICLOUD_EMAIL,
ICLOUD_APP_SPECIFIC_PASSWORD. PORT is injected by Railway.
"""
import hmac
import os

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from icloud_mcp.server import mcp

SECRET_PATH = os.environ.get("MCP_URL_SECRET", "").strip()
BEARER = os.environ.get("MCP_BEARER_TOKEN", "").strip()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Mode B gate: require Authorization: Bearer <MCP_BEARER_TOKEN>."""
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not BEARER:
            return JSONResponse(
                {"error": "server misconfigured: set MCP_URL_SECRET or MCP_BEARER_TOKEN"},
                status_code=500,
            )
        provided = request.headers.get("authorization", "")
        if not (provided and hmac.compare_digest(provided, "Bearer %s" % BEARER)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def main():
    port = int(os.environ.get("PORT", "8000"))

    if SECRET_PATH:
        # Mode A: gate by unguessable URL path. No header needed.
        mcp_path = "/%s/mcp" % SECRET_PATH
        app = mcp.http_app(path=mcp_path)
        print("[run_readonly] MODE A (URL-path secret). MCP served at %s" % mcp_path, flush=True)
    else:
        # Mode B: gate by bearer header.
        app = mcp.http_app(path="/mcp")
        app.add_middleware(BearerAuthMiddleware)
        print("[run_readonly] MODE B (bearer header). MCP served at /mcp", flush=True)

    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

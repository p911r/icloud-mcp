#!/usr/bin/env python3
"""
Read-only, token-gated HTTP entrypoint for the iCloud MCP fork.

mike-tih/icloud-mcp ships with NO transport authentication — it reads iCloud
creds from env/headers and exposes /mcp openly. On a public Railway URL that
holds your iCloud app-specific password, that is unacceptable. This entrypoint
wraps the FastMCP streamable-HTTP app with a constant-time bearer-token check.

Auth model:
  - /health           -> always open (Railway/Docker health check)
  - everything else    -> requires  Authorization: Bearer <MCP_BEARER_TOKEN>

Env:
  PORT               listen port (Railway injects this; default 8000)
  MCP_BEARER_TOKEN   required shared secret; if unset the server refuses traffic
  ICLOUD_EMAIL                  iCloud address (Railway secret)
  ICLOUD_APP_SPECIFIC_PASSWORD dedicated, revocable app password (Railway secret)

If the Cowork "add custom connector" dialog cannot attach an Authorization
header, see the runbook for the URL-path-secret fallback.
"""
import hmac
import os

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from icloud_mcp.server import mcp

BEARER = os.environ.get("MCP_BEARER_TOKEN", "").strip()


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not BEARER:
            return JSONResponse(
                {"error": "server misconfigured: MCP_BEARER_TOKEN is unset"},
                status_code=500,
            )
        provided = request.headers.get("authorization", "")
        expected = "Bearer %s" % BEARER
        if not (provided and hmac.compare_digest(provided, expected)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def main():
    port = int(os.environ.get("PORT", "8000"))
    # Streamable HTTP app; MCP endpoint served at /mcp (FastMCP default).
    app = mcp.http_app(path="/mcp")
    app.add_middleware(BearerAuthMiddleware)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

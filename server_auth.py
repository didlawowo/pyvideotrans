"""Authentification partagée : Bearer pour Hermes, cookie HttpOnly pour le navigateur."""

import hashlib
import hmac

from starlette.responses import JSONResponse, RedirectResponse
from starlette.requests import Request


def browser_cookie(token: str) -> str:
    return hmac.new(
        token.encode(), b"pyvideotrans-browser-v1", hashlib.sha256
    ).hexdigest()


class AuthMiddleware:
    def __init__(self, app, token: str):
        if not token.strip():
            raise ValueError("MCP_AUTH_TOKEN est obligatoire")
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        path = request.url.path
        if path in ("/healthz", "/login"):
            return await self.app(scope, receive, send)
        scheme, _, creds = request.headers.get("authorization", "").partition(" ")
        bearer = scheme.lower() == "bearer" and hmac.compare_digest(
            creds.encode(), self.token.encode()
        )
        cookie = hmac.compare_digest(
            request.cookies.get("pvt_session", "").encode(),
            browser_cookie(self.token).encode(),
        )
        # MCP always requires Bearer. Browser mutations also require same origin
        # when Origin is present; SameSite=strict protects the session cookie.
        is_mcp = path == "/mcp" or path.startswith("/mcp/")
        origin = request.headers.get("origin")
        same_origin = not origin or origin == str(request.base_url).rstrip("/")
        if not bearer and (is_mcp or not cookie or not same_origin):
            response = (
                RedirectResponse("/login", status_code=303)
                if path == "/"
                else JSONResponse(
                    {"detail": "Authentification requise"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            )
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)

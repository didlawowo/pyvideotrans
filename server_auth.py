"""Authentification partagée : Bearer pour Hermes, cookie HttpOnly pour le navigateur."""

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, RedirectResponse
from starlette.requests import Request

# Secret de session tiré au démarrage, jamais dérivé de MCP_AUTH_TOKEN : un
# cookie volé ne révèle rien du Bearer, et un redémarrage du processus invalide
# toutes les sessions navigateur (seule forme de révocation disponible ici,
# cohérente avec l'état des tâches qui est lui aussi en mémoire).
_SESSION_SECRET = secrets.token_bytes(32)


def browser_cookie(token: str) -> str:
    return hmac.new(_SESSION_SECRET, token.encode(), hashlib.sha256).hexdigest()


def _same_origin(origin: str, request: Request) -> bool:
    """Compare l'hôte, pas l'URL complète.

    Derrière Traefik, uvicorn ne fait confiance aux en-têtes X-Forwarded-* que
    si `forwarded_allow_ips` couvre l'IP du proxy ; sinon `request.base_url`
    reste en `http://` alors que le navigateur annonce `Origin: https://...`,
    et toute mutation navigateur serait rejetée. L'hôte, lui, vient du Host et
    reste juste dans les deux cas. Une origine d'un autre hôte reste refusée.
    """
    if not origin:
        return True
    parts = urlsplit(origin)
    return bool(parts.netloc) and parts.netloc == request.url.netloc


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
        same_origin = _same_origin(request.headers.get("origin", ""), request)
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

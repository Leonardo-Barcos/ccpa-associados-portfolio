"""Application entry point — CCPA membership signup."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import settings
from .database import ensure_schema, init_db
from .paths import STATIC_DIR
from .routers import membership, painel, webhooks
from .templating import templates

logging.basicConfig(
    level=logging.INFO if settings.is_prod else logging.DEBUG,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    if settings.auto_create_tables:
        init_db()
    else:
        # Even with table creation off, a column added in this release must
        # exist before the first query — otherwise a deploy that ran ahead of
        # `scripts/init_db.py` would take the whole signup down. It is one
        # catalogue read when there is nothing to do.
        try:
            added = ensure_schema()
            if added:
                logging.getLogger("ccpa").warning("Schema updated: %s", added)
        except Exception:  # noqa: BLE001
            logging.getLogger("ccpa").exception("ensure_schema failed")
    yield


app = FastAPI(
    title="CCPA — associados",
    # The interactive API docs are a development convenience, not something to
    # expose on a public site.
    docs_url=None if settings.is_prod else "/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(membership.router)
app.include_router(webhooks.router)
app.include_router(painel.router)


@app.exception_handler(painel.LoginRequired)
async def _login_required(request: Request, exc: painel.LoginRequired):  # noqa: ARG001
    return RedirectResponse(
        f"/painel/entrar?proxima={quote(exc.next_path)}", status_code=303
    )


@app.exception_handler(painel.PasswordChangeRequired)
async def _password_change(request: Request, exc):  # noqa: ARG001
    return RedirectResponse("/painel/senha", status_code=303)


@app.exception_handler(painel.Forbidden)
async def _forbidden(request: Request, exc):  # noqa: ARG001
    return templates.TemplateResponse(
        request, "painel/proibido.html", {"request": request}, status_code=403
    )


def canonical_url(request: Request) -> str | None:
    """Where a visitor on the bare *.vercel.app address should be sent.

    The project answers on ccpa-membros.vercel.app as well as on the
    association's own domain, and a signup link on vercel.app does not look
    like the club. Only the production deployment redirects: preview
    deployments live on vercel.app by design. The webhook stays put, because
    Asaas does not follow redirects and a lost payment confirmation is a
    member who paid and was never activated.
    """
    if os.getenv("VERCEL_ENV") != "production":
        return None
    canonical = urlsplit(settings.base_url).netloc
    host = request.headers.get("host", "")
    if not canonical or host == canonical or not host.endswith(".vercel.app"):
        return None
    if request.method not in ("GET", "HEAD"):
        return None
    if request.url.path.startswith(("/webhooks", "/healthz")):
        return None
    query = f"?{request.url.query}" if request.url.query else ""
    return f"https://{canonical}{request.url.path}{query}"


@app.middleware("http")
async def canonical_host(request: Request, call_next):
    target = canonical_url(request)
    if target:
        return RedirectResponse(target, status_code=308)
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self'; "
        "connect-src 'self' https://viacep.com.br; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/painel"):
        # Member data: never cached by the browser or a proxy, never indexed.
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    if settings.is_prod:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


@app.get("/", include_in_schema=False)
def root():
    """This deployment is the signup funnel only; the institutional site lives
    elsewhere. The root is a redirect so a bare domain still lands somewhere
    useful."""
    return RedirectResponse("/associe-se", status_code=307)


@app.exception_handler(404)
async def not_found(request: Request, exc):  # noqa: ARG001
    """Send visitors back to the form; answer API callers with JSON."""
    if request.url.path.startswith(("/webhooks", "/healthz")):
        return JSONResponse({"detail": "not found"}, status_code=404)
    if request.url.path.startswith("/painel"):
        return RedirectResponse("/painel", status_code=307)
    return RedirectResponse("/associe-se", status_code=307)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return JSONResponse({"status": "ok"})

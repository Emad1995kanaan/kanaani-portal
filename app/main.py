from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import settings
from .deps import AuthRequired, CsrfFailed, Forbidden, PasswordChangeRequired
from .routers import admin, api, auth, reseller
from .security import client_ip, rate_limiter
from .web import templates

BASE = Path(__file__).parent

app = FastAPI(
    title=f"{settings.app_name} API", version=__version__,
    docs_url="/api/docs" if settings.enable_api_docs else None,
    redoc_url=None, openapi_url="/api/openapi.json" if settings.enable_api_docs else None,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'")


@app.middleware("http")
async def security_middleware(request: Request, call_next):  # noqa: ANN001, ANN201
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        key = client_ip(request)
        if not rate_limiter.allow(key) or (request.url.path == "/login" and not rate_limiter.allow("login:" + key)):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "Too many requests. Slow down."}, status_code=429)
            return HTMLResponse("Too many requests. Please wait a minute.", status_code=429)
    response = await call_next(request)
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if settings.cookie_secure:
        h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if not request.url.path.startswith("/static/"):
        h.setdefault("Cache-Control", "no-store")
    return response


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/")


@app.exception_handler(AuthRequired)
async def _auth_required(request: Request, _exc: AuthRequired):  # noqa: ANN202
    if _wants_json(request):
        return JSONResponse({"detail": "Not signed in."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(Forbidden)
async def _forbidden(request: Request, _exc: Forbidden):  # noqa: ANN202
    if _wants_json(request):
        return JSONResponse({"detail": "Not allowed."}, status_code=403)
    return templates.TemplateResponse(request, "error.html", {"code": 403, "message": "You don't have access to this page."}, status_code=403)


@app.exception_handler(PasswordChangeRequired)
async def _pw_change(request: Request, _exc: PasswordChangeRequired):  # noqa: ANN202
    if _wants_json(request):
        return JSONResponse({"detail": "Change your temporary password first."}, status_code=403)
    return RedirectResponse("/account/password", status_code=303)


@app.exception_handler(CsrfFailed)
async def _csrf(request: Request, _exc: CsrfFailed):  # noqa: ANN202
    if _wants_json(request):
        return JSONResponse({"detail": "Security token missing or expired. Reload and try again."}, status_code=403)
    return templates.TemplateResponse(request, "error.html", {"code": 403, "message": "Security token missing or expired. Go back, reload the page and try again."}, status_code=403)


@app.exception_handler(404)
async def _not_found(request: Request, _exc):  # noqa: ANN001, ANN202
    if _wants_json(request):
        return JSONResponse({"detail": "Not found."}, status_code=404)
    return templates.TemplateResponse(request, "error.html", {"code": 404, "message": "Page not found."}, status_code=404)


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True, "version": __version__}


app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(reseller.router)
app.include_router(api.router)

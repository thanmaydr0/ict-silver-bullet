"""Same-origin session API and production Vite assets on the existing port."""
from contextlib import asynccontextmanager
import json
import os
from hashlib import sha256
from ipaddress import ip_address
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import dotenv_values

from brain.dashboard_auth import COOKIE, TTL_SECONDS, LoginThrottled, Sessions
from brain.dashboard_data import Collector
from brain.dashboard_models import DashboardSnapshot, Login, SessionInfo

DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


def read_build(path):
    try:
        build = json.loads((path / "build.json").read_text(encoding="utf-8"))
        index = (path / "index.html").read_bytes()
        if build["schema_version"] != 1 or build["build_id"] != sha256(index).hexdigest()[:16]:
            raise ValueError("Invalid build")
        if not (path / "assets").is_dir():
            raise ValueError("Assets missing")
        for asset in re.findall(r'(?:src|href)="(/assets/[^"?]+)"', index.decode("utf-8")):
            file = (path / asset.lstrip("/")).resolve()
            if path.resolve() not in file.parents or not file.is_file():
                raise ValueError("Referenced asset missing")
        return build
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Vite build missing or invalid. Build frontend before starting the dashboard.") from exc


def runtime_options():
    values = dotenv_values(Path(__file__).with_name(".env"), interpolate=False)
    mode = os.environ.get("DASHBOARD_COOKIE_SECURE", values.get("DASHBOARD_COOKIE_SECURE") or "false").lower()
    if mode not in ("true", "false"):
        raise RuntimeError("DASHBOARD_COOKIE_SECURE must be true or false")
    proxy = os.environ.get("DASHBOARD_TRUSTED_PROXY_IP", values.get("DASHBOARD_TRUSTED_PROXY_IP") or "")
    if proxy:
        try:
            proxy = str(ip_address(proxy))
        except ValueError:
            raise RuntimeError("DASHBOARD_TRUSTED_PROXY_IP must be one explicit proxy IP") from None
    return mode == "true", proxy


def create_dashboard(config, *, collector=None, sessions=None, dist=None, development=False):
    if not config.DASHBOARD_USERNAME or not config.DASHBOARD_PASSWORD:
        raise RuntimeError("Dashboard username and password are required")
    path = Path(dist) if dist is not None else DIST
    build = {"build_id": "development"} if development else read_build(path)
    collector = collector or Collector()
    sessions = sessions or Sessions(config.DASHBOARD_USERNAME, config.DASHBOARD_PASSWORD)
    secure, _ = runtime_options()

    @asynccontextmanager
    async def lifespan(app):
        collector.start()
        try:
            yield
        finally:
            collector.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if request.url.path.startswith("/assets/") and response.status_code == 200 else "no-store"
        return response

    def user(request: Request):
        result = sessions.check(request.cookies.get(COOKIE))
        if result is None:
            raise HTTPException(401, "Session required")
        return result

    def origin(request: Request):
        source = urlsplit(request.headers.get("origin", ""))
        if source.scheme not in ("http", "https") or source.netloc != request.url.netloc or source.scheme != request.url.scheme or source.path or source.query or source.fragment:
            raise HTTPException(403, "Same-origin request required")

    @app.post("/api/v1/auth/login", response_model=SessionInfo, dependencies=[Depends(origin)])
    def login(body: Login, request: Request, response: Response):
        if secure and request.url.scheme != "https":
            raise HTTPException(400, "HTTPS required for secure cookies")
        try:
            result = sessions.login(body.username, body.password, request.client.host if request.client else "unknown", request.cookies.get(COOKIE))
        except LoginThrottled:
            raise HTTPException(429, "Too many attempts. Retry in five minutes.", headers={"Retry-After": "300"})
        if result is None:
            raise HTTPException(401, "Invalid username or password")
        token, info = result
        response.set_cookie(COOKIE, token, max_age=TTL_SECONDS, httponly=True, secure=secure, samesite="strict", path="/")
        return info

    @app.get("/api/v1/auth/session", response_model=SessionInfo)
    def session(info=Depends(user)):
        return info

    @app.post("/api/v1/auth/logout", status_code=204, dependencies=[Depends(origin)])
    def logout(request: Request):
        sessions.logout(request.cookies.get(COOKIE))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/", secure=secure, httponly=True, samesite="strict")
        return response

    @app.get("/api/v1/dashboard", response_model=DashboardSnapshot, dependencies=[Depends(user)])
    def dashboard():
        return collector.snapshot()

    @app.get("/healthz")
    def health():
        return {"status": "ok", "schema_version": 1, "build_id": build["build_id"]}

    if not development:
        app.mount("/assets", StaticFiles(directory=path / "assets"), name="assets")
        for route in ("/", "/login", "/dashboard"):
            app.add_api_route(route, lambda: FileResponse(path / "index.html", media_type="text/html"), methods=["GET"], include_in_schema=False)
    return app

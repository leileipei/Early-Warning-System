from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import LOGIN_FAILURE_DETAIL, LOGIN_LIMIT_DETAIL, router as auth_router
from app.db import get_engine
from app.health import ReadinessResult, check_readiness
from app.paths import STATIC_DIR
from app.routes import router as page_router, templates
from app.settings import get_settings
from app.web_security import (
    LoginRateLimiter,
    SecurityHeadersMiddleware,
    apply_security_headers,
    ensure_csrf_token,
)


def _quality_value(parameters: list[str]) -> float:
    for parameter in parameters:
        name, separator, value = parameter.partition("=")
        if name.strip().lower() != "q":
            continue
        if not separator:
            return 0.0
        try:
            quality = float(value.strip())
        except ValueError:
            return 0.0
        return quality if 0.0 <= quality <= 1.0 else 0.0
    return 1.0


def _media_quality(accept_header: str, target: str) -> float:
    target_type, target_subtype = target.split("/", 1)
    best_specificity = -1
    best_quality = 0.0

    for item in accept_header.split(","):
        media_range, *parameters = item.split(";")
        media_type, separator, media_subtype = media_range.strip().lower().partition("/")
        if not separator or media_type not in {"*", target_type}:
            continue
        if media_subtype not in {"*", target_subtype}:
            continue

        specificity = int(media_type != "*") + int(media_subtype != "*")
        quality = _quality_value(parameters)
        if specificity > best_specificity:
            best_specificity = specificity
            best_quality = quality
        elif specificity == best_specificity:
            best_quality = max(best_quality, quality)

    return best_quality


def _prefers_html(accept_header: str) -> bool:
    html_quality = _media_quality(accept_header, "text/html")
    json_quality = _media_quality(accept_header, "application/json")
    return html_quality > 0.0 and html_quality > json_quality


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)
    app.state.login_rate_limiter = LoginRateLimiter(
        max_failures=settings.login_max_failures,
        failure_window_seconds=settings.login_failure_window_seconds,
        lockout_seconds=settings.login_lockout_seconds,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        https_only=settings.session_cookie_secure,
        same_site="lax",
        max_age=settings.session_max_age_seconds,
    )
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=settings.session_cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth_router)
    app.include_router(page_router)

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException):
        accepts_html = _prefers_html(request.headers.get("accept", ""))
        if (
            request.url.path == "/login"
            and request.method == "POST"
            and accepts_html
            and exc.status_code in {
                status.HTTP_400_BAD_REQUEST,
                status.HTTP_429_TOO_MANY_REQUESTS,
            }
        ):
            error = (
                LOGIN_FAILURE_DETAIL
                if exc.status_code == status.HTTP_400_BAD_REQUEST
                else LOGIN_LIMIT_DETAIL
            )
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "request": request,
                    "csrf_token": ensure_csrf_token(request),
                    "title": "登录",
                    "error": error,
                },
                status_code=exc.status_code,
                headers=dict(exc.headers or {}),
            )
        if (
            exc.status_code == status.HTTP_401_UNAUTHORIZED
            and request.method == "GET"
            and accepts_html
        ):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return await http_exception_handler(request, exc)

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(_request: Request, _exc: Exception):
        # ServerErrorMiddleware renders unhandled exceptions outside user middleware.
        response = PlainTextResponse("Internal Server Error", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return apply_security_headers(response, enable_hsts=settings.session_cookie_secure)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def readiness(response: Response) -> dict[str, object]:
        try:
            engine = get_engine()
        except Exception:
            result = ReadinessResult(
                False, {"database": "unavailable", "worker": "unknown"}
            )
        else:
            result = check_readiness(
                engine,
                heartbeat_timeout_seconds=settings.worker_heartbeat_timeout_seconds,
            )
        if not result.ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "ready" if result.ready else "not_ready",
            "components": result.components,
        }

    return app


app = create_app()

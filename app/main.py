from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import router as auth_router
from app.paths import STATIC_DIR
from app.routes import router as page_router
from app.settings import get_settings
from app.web_security import LoginRateLimiter


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
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth_router)
    app.include_router(page_router)

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException):
        accepts_html = "text/html" in request.headers.get("accept", "")
        if (
            exc.status_code == status.HTTP_401_UNAUTHORIZED
            and request.method == "GET"
            and accepts_html
        ):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return await http_exception_handler(request, exc)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import AdminUser
from app.security import verify_password
from app.settings import get_settings
from app.web_security import (
    LoginRateLimiter,
    client_identifier,
    require_csrf,
    rotate_csrf_token,
)

router = APIRouter(dependencies=[Depends(require_csrf)])
LOGIN_FAILURE_DETAIL = "用户名或密码错误"
LOGIN_LIMIT_DETAIL = "登录尝试过多，请稍后重试"
DUMMY_PASSWORD_HASH = "$2b$12$0kBPCpi0b/V47sHVriEA7.Du9xSBpCUp8Dmb5xq.fDZVF3Fc0ZyKW"
ADMIN_USER_ID_SESSION_KEY = "admin_user_id"
AUTHENTICATED_AT_SESSION_KEY = "authenticated_at"
LAST_ACTIVITY_AT_SESSION_KEY = "last_activity_at"
ADMIN_SESSION_VERSION_SESSION_KEY = "admin_session_version"


def _epoch_seconds(now: datetime | None) -> int:
    return int((now or datetime.now(UTC)).timestamp())


def _session_integer(request: Request, key: str) -> int | None:
    value = request.session.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _invalidate_admin_session(request: Request) -> bool:
    request.session.clear()
    return False


def establish_admin_session(
    request: Request,
    user: AdminUser,
    *,
    now: datetime | None = None,
) -> None:
    if user.id is None:
        raise ValueError("admin user must be persisted before establishing a session")

    epoch_seconds = _epoch_seconds(now)
    request.session[ADMIN_USER_ID_SESSION_KEY] = user.id
    request.session[AUTHENTICATED_AT_SESSION_KEY] = epoch_seconds
    request.session[LAST_ACTIVITY_AT_SESSION_KEY] = epoch_seconds
    request.session[ADMIN_SESSION_VERSION_SESSION_KEY] = user.session_version


def validate_admin_session(
    request: Request,
    user: AdminUser,
    *,
    max_age_seconds: int,
    idle_timeout_seconds: int,
    now: datetime | None = None,
) -> bool:
    user_id = _session_integer(request, ADMIN_USER_ID_SESSION_KEY)
    authenticated_at = _session_integer(request, AUTHENTICATED_AT_SESSION_KEY)
    last_activity_at = _session_integer(request, LAST_ACTIVITY_AT_SESSION_KEY)
    session_version = _session_integer(request, ADMIN_SESSION_VERSION_SESSION_KEY)
    current_time = _epoch_seconds(now)

    if (
        user_id is None
        or authenticated_at is None
        or last_activity_at is None
        or session_version is None
        or user.id is None
        or user_id != user.id
        or session_version != user.session_version
        or authenticated_at > current_time
        or last_activity_at > current_time
        or last_activity_at < authenticated_at
        or current_time - authenticated_at >= max_age_seconds
        or current_time - last_activity_at >= idle_timeout_seconds
    ):
        return _invalidate_admin_session(request)

    request.session[LAST_ACTIVITY_AT_SESSION_KEY] = current_time
    return True


def _raise_login_limited(retry_after: int) -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=LOGIN_LIMIT_DETAIL,
        headers={"Retry-After": str(retry_after)},
    )


def require_admin(
    request: Request,
    session: Session = Depends(get_session),
) -> AdminUser:
    user_id = _session_integer(request, ADMIN_USER_ID_SESSION_KEY)
    if user_id is None:
        _invalidate_admin_session(request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = session.get(AdminUser, user_id)
    if not user:
        _invalidate_admin_session(request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    settings = get_settings()
    if not validate_admin_session(
        request,
        user,
        max_age_seconds=settings.session_max_age_seconds,
        idle_timeout_seconds=settings.session_idle_timeout_seconds,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    client_id = client_identifier(request)
    with limiter.attempt(client_id, username) as retry_after:
        if retry_after:
            _raise_login_limited(retry_after)

        user = session.exec(select(AdminUser).where(AdminUser.username == username)).first()
        password_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
        password_matches = verify_password(password, password_hash)
        if user is None or not password_matches:
            retry_after = limiter.record_failure(client_id, username)
            if retry_after:
                _raise_login_limited(retry_after)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=LOGIN_FAILURE_DETAIL)

        limiter.clear(client_id, username)
        request.session.clear()
        rotate_csrf_token(request)
        establish_admin_session(request, user)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

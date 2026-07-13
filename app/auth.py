from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import AdminUser
from app.security import verify_password
from app.web_security import (
    LoginRateLimiter,
    client_identifier,
    require_csrf,
    rotate_csrf_token,
)

router = APIRouter(dependencies=[Depends(require_csrf)])
LOGIN_LIMIT_DETAIL = "登录尝试过多，请稍后重试"
DUMMY_PASSWORD_HASH = "$2b$12$0kBPCpi0b/V47sHVriEA7.Du9xSBpCUp8Dmb5xq.fDZVF3Fc0ZyKW"


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
    admin_user_id = request.session.get("admin_user_id")
    if admin_user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        user_id = int(admin_user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from exc
    user = session.get(AdminUser, user_id)
    if not user:
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
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名或密码错误")

        limiter.clear(client_id, username)
        request.session.clear()
        rotate_csrf_token(request)
        request.session["admin_user_id"] = user.id
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

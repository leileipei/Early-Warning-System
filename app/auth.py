from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.db import get_session
from app.models import AdminUser
from app.security import verify_password
from app.web_security import require_csrf, rotate_csrf_token

router = APIRouter(dependencies=[Depends(require_csrf)])


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
    user = session.exec(select(AdminUser).where(AdminUser.username == username)).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名或密码错误")
    request.session.clear()
    request.session["admin_user_id"] = user.id
    rotate_csrf_token(request)
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

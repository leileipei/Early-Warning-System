import secrets

from fastapi import HTTPException, Request, status

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "_csrf_token"
CSRF_ERROR = "请求安全校验失败"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def rotate_csrf_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    request.session[CSRF_SESSION_KEY] = token
    return token


async def require_csrf(request: Request) -> None:
    if request.method.upper() not in UNSAFE_METHODS:
        return

    expected = request.session.get(CSRF_SESSION_KEY)
    try:
        form = await request.form()
        provided = form.get(CSRF_FORM_FIELD)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_ERROR) from exc

    if (
        not isinstance(expected, str)
        or not expected
        or not isinstance(provided, str)
        or not secrets.compare_digest(expected, provided)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CSRF_ERROR)

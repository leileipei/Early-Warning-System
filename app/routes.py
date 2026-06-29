from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_admin
from app.models import AdminUser

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "title": "登录"})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, admin: AdminUser = Depends(require_admin)):
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "admin": admin, "title": "仪表盘"},
    )


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, admin: AdminUser = Depends(require_admin)):
    return templates.TemplateResponse(
        "rules.html",
        {"request": request, "admin": admin, "title": "预警规则"},
    )


@router.get("/rules/new", response_class=HTMLResponse)
def new_rule_page(request: Request, admin: AdminUser = Depends(require_admin)):
    return templates.TemplateResponse(
        "rule_form.html",
        {"request": request, "admin": admin, "title": "新建规则"},
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, admin: AdminUser = Depends(require_admin)):
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "admin": admin, "title": "系统配置"},
    )


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, admin: AdminUser = Depends(require_admin)):
    return templates.TemplateResponse(
        "logs.html",
        {"request": request, "admin": admin, "title": "日志"},
    )

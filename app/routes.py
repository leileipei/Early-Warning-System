from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import require_admin
from app.crypto import SecretCipher
from app.db import get_session
from app.execution_service import execute_rule_by_id
from app.models import (
    AdminUser,
    AlertRule,
    ExecutionLog,
    MailLog,
    SendMode,
    SmtpConfig,
    SqlDataSource,
    TriggerType,
)
from app.paths import TEMPLATES_DIR
from app.settings import get_settings
from app.sql_validator import SqlValidationError, validate_select_only_sql

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _template_response(
    request: Request,
    template_name: str,
    context: dict,
    *,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        template_name,
        {"request": request, **context},
        status_code=status_code,
    )


def _is_checked(value: str | None) -> bool:
    return value in {"on", "true", "1"}


def _cipher() -> SecretCipher:
    return SecretCipher.from_key_material(get_settings().secret_key)


def _rule_form_context(
    request: Request,
    admin: AdminUser,
    session: Session,
    *,
    error: str = "",
    form: dict[str, str] | None = None,
) -> dict:
    data_sources = session.exec(select(SqlDataSource).order_by(SqlDataSource.name)).all()
    return {
        "request": request,
        "admin": admin,
        "title": "新建规则",
        "data_sources": data_sources,
        "send_modes": list(SendMode),
        "error": error,
        "form": form or {},
    }


def _settings_context(
    request: Request,
    admin: AdminUser,
    session: Session,
    *,
    error: str = "",
) -> dict:
    data_sources = session.exec(select(SqlDataSource).order_by(SqlDataSource.created_at.desc())).all()
    smtp_configs = session.exec(select(SmtpConfig).order_by(SmtpConfig.updated_at.desc())).all()
    return {
        "request": request,
        "admin": admin,
        "title": "系统配置",
        "data_sources": data_sources,
        "smtp_configs": smtp_configs,
        "error": error,
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _template_response(request, "login.html", {"title": "登录"})


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, admin: AdminUser = Depends(require_admin)):
    return _template_response(
        request,
        "dashboard.html",
        {"admin": admin, "title": "仪表盘"},
    )


@router.get("/rules", response_class=HTMLResponse)
def rules_page(
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    rules = session.exec(select(AlertRule).order_by(AlertRule.created_at.desc())).all()
    return _template_response(
        request,
        "rules.html",
        {"admin": admin, "title": "预警规则", "rules": rules},
    )


@router.get("/rules/new", response_class=HTMLResponse)
def new_rule_page(
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _template_response(
        request,
        "rule_form.html",
        _rule_form_context(request, admin, session),
    )


@router.post("/rules")
def create_rule(
    request: Request,
    name: str = Form(""),
    data_source_id: str = Form(""),
    sql_text: str = Form(""),
    cron_expression: str = Form(""),
    recipients: str = Form(""),
    cc_recipients: str = Form(""),
    subject_template: str = Form(""),
    body_template: str = Form(""),
    send_mode: str = Form(SendMode.SUMMARY.value),
    query_timeout_seconds: int = Form(30),
    max_rows: int = Form(500),
    enabled: str | None = Form(None),
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    form = {
        "name": name,
        "data_source_id": data_source_id,
        "sql_text": sql_text,
        "cron_expression": cron_expression,
        "recipients": recipients,
        "cc_recipients": cc_recipients,
        "subject_template": subject_template,
        "body_template": body_template,
        "send_mode": send_mode,
        "query_timeout_seconds": str(query_timeout_seconds),
        "max_rows": str(max_rows),
        "enabled": "on" if _is_checked(enabled) else "",
    }

    try:
        validate_select_only_sql(sql_text)
        parsed_send_mode = SendMode(send_mode)
    except (SqlValidationError, ValueError) as exc:
        message = str(exc) or "表单数据无效"
        return _template_response(
            request,
            "rule_form.html",
            _rule_form_context(request, admin, session, error=message, form=form),
            status_code=400,
        )

    try:
        CronTrigger.from_crontab(cron_expression)
    except ValueError:
        return _template_response(
            request,
            "rule_form.html",
            _rule_form_context(request, admin, session, error="Cron 表达式无效", form=form),
            status_code=400,
        )

    try:
        source_id = int(data_source_id)
    except ValueError:
        return _template_response(
            request,
            "rule_form.html",
            _rule_form_context(request, admin, session, error="请选择有效的数据源", form=form),
            status_code=400,
        )

    if session.get(SqlDataSource, source_id) is None:
        return _template_response(
            request,
            "rule_form.html",
            _rule_form_context(request, admin, session, error="请选择有效的数据源", form=form),
            status_code=400,
        )

    rule = AlertRule(
        name=name,
        data_source_id=source_id,
        sql_text=sql_text,
        cron_expression=cron_expression,
        recipients=recipients,
        cc_recipients=cc_recipients,
        subject_template=subject_template,
        body_template=body_template,
        send_mode=parsed_send_mode,
        query_timeout_seconds=query_timeout_seconds,
        max_rows=max_rows,
        enabled=_is_checked(enabled),
    )
    session.add(rule)
    session.commit()
    return RedirectResponse("/rules", status_code=303)


@router.post("/rules/{rule_id}/run")
def run_rule(
    rule_id: int,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    if session.get(AlertRule, rule_id) is None:
        raise HTTPException(status_code=404, detail="规则不存在")

    execute_rule_by_id(session, rule_id, trigger_type=TriggerType.MANUAL)
    return RedirectResponse("/logs", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _template_response(
        request,
        "settings.html",
        _settings_context(request, admin, session),
    )


@router.post("/settings/sql-server")
def create_sql_server_settings(
    request: Request,
    name: str = Form(""),
    host: str = Form(""),
    port: int = Form(1433),
    database: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    enabled: str | None = Form(None),
    connect_timeout_seconds: int = Form(10),
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    existing = session.exec(select(SqlDataSource).where(SqlDataSource.name == name)).first()
    if existing is not None:
        return _template_response(
            request,
            "settings.html",
            _settings_context(request, admin, session, error="数据源名称已存在"),
            status_code=400,
        )

    data_source = SqlDataSource(
        name=name,
        host=host,
        port=port,
        database=database,
        username=username,
        encrypted_password=_cipher().encrypt(password),
        enabled=_is_checked(enabled),
        connect_timeout_seconds=connect_timeout_seconds,
    )
    session.add(data_source)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/smtp")
def create_smtp_settings(
    host: str = Form(""),
    port: int = Form(587),
    username: str = Form(""),
    password: str = Form(""),
    sender: str = Form(""),
    use_tls: str | None = Form(None),
    use_ssl: str | None = Form(None),
    enabled: str | None = Form(None),
    timeout_seconds: int = Form(10),
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    smtp_config = SmtpConfig(
        host=host,
        port=port,
        username=username,
        encrypted_password=_cipher().encrypt(password),
        sender=sender,
        use_tls=_is_checked(use_tls),
        use_ssl=_is_checked(use_ssl),
        enabled=_is_checked(enabled),
        timeout_seconds=timeout_seconds,
    )
    session.add(smtp_config)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    execution_logs = session.exec(select(ExecutionLog).order_by(ExecutionLog.started_at.desc())).all()
    mail_logs = session.exec(select(MailLog).order_by(MailLog.sent_at.desc())).all()
    return _template_response(
        request,
        "logs.html",
        {
            "admin": admin,
            "title": "日志",
            "execution_logs": execution_logs,
            "mail_logs": mail_logs,
        },
    )

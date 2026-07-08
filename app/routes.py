from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import require_admin
from app.crypto import SecretCipher
from app.db import get_session
from app.execution_service import build_sql_client, execute_rule_by_id
from app.models import (
    AdminUser,
    AlertRule,
    ExecutionLog,
    MailLog,
    SendMode,
    SmtpConfig,
    SqlDataSource,
    TriggerType,
    utc_now,
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
    action: str = "/rules",
    heading: str = "新建规则",
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
        "action": action,
        "heading": heading,
    }


def _rule_to_form(rule: AlertRule) -> dict[str, str]:
    return {
        "name": rule.name,
        "data_source_id": str(rule.data_source_id),
        "sql_text": rule.sql_text,
        "cron_expression": rule.cron_expression,
        "recipients": rule.recipients,
        "cc_recipients": rule.cc_recipients,
        "subject_template": rule.subject_template,
        "body_template": rule.body_template,
        "send_mode": rule.send_mode.value,
        "query_timeout_seconds": str(rule.query_timeout_seconds),
        "max_rows": str(rule.max_rows),
        "enabled": "on" if rule.enabled else "",
    }


def _submitted_rule_form(
    name: str,
    data_source_id: str,
    sql_text: str,
    cron_expression: str,
    recipients: str,
    cc_recipients: str,
    subject_template: str,
    body_template: str,
    send_mode: str,
    query_timeout_seconds: int,
    max_rows: int,
    enabled: str | None,
) -> dict[str, str]:
    return {
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


def _validate_rule_form(
    request: Request,
    admin: AdminUser,
    session: Session,
    *,
    form: dict[str, str],
    action: str,
    heading: str,
):
    try:
        validate_select_only_sql(form["sql_text"])
        parsed_send_mode = SendMode(form["send_mode"])
    except (SqlValidationError, ValueError) as exc:
        message = str(exc) or "表单数据无效"
        return None, None, _template_response(
            request,
            "rule_form.html",
            _rule_form_context(
                request,
                admin,
                session,
                error=message,
                form=form,
                action=action,
                heading=heading,
            ),
            status_code=400,
        )

    try:
        CronTrigger.from_crontab(form["cron_expression"])
    except ValueError:
        return None, None, _template_response(
            request,
            "rule_form.html",
            _rule_form_context(
                request,
                admin,
                session,
                error="Cron 表达式无效",
                form=form,
                action=action,
                heading=heading,
            ),
            status_code=400,
        )

    try:
        source_id = int(form["data_source_id"])
    except ValueError:
        return None, None, _template_response(
            request,
            "rule_form.html",
            _rule_form_context(
                request,
                admin,
                session,
                error="请选择有效的数据源",
                form=form,
                action=action,
                heading=heading,
            ),
            status_code=400,
        )

    if session.get(SqlDataSource, source_id) is None:
        return None, None, _template_response(
            request,
            "rule_form.html",
            _rule_form_context(
                request,
                admin,
                session,
                error="请选择有效的数据源",
                form=form,
                action=action,
                heading=heading,
            ),
            status_code=400,
        )

    return source_id, parsed_send_mode, None


def _settings_context(
    request: Request,
    admin: AdminUser,
    session: Session,
    *,
    error: str = "",
    notice: str = "",
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
        "notice": notice,
    }


def _data_source_to_form(data_source: SqlDataSource) -> dict[str, str]:
    return {
        "name": data_source.name,
        "host": data_source.host,
        "port": str(data_source.port),
        "database": data_source.database,
        "username": data_source.username,
        "password": "",
        "enabled": "on" if data_source.enabled else "",
        "connect_timeout_seconds": str(data_source.connect_timeout_seconds),
        "odbc_driver": data_source.odbc_driver,
        "server_override": data_source.server_override,
        "encrypt": data_source.encrypt,
        "trust_server_certificate": data_source.trust_server_certificate,
        "extra_params": data_source.extra_params,
    }


def _submitted_data_source_form(
    name: str,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    enabled: str | None,
    connect_timeout_seconds: int,
    odbc_driver: str,
    server_override: str,
    encrypt: str,
    trust_server_certificate: str,
    extra_params: str,
) -> dict[str, str]:
    return {
        "name": name,
        "host": host,
        "port": str(port),
        "database": database,
        "username": username,
        "password": password,
        "enabled": "on" if _is_checked(enabled) else "",
        "connect_timeout_seconds": str(connect_timeout_seconds),
        "odbc_driver": odbc_driver,
        "server_override": server_override,
        "encrypt": encrypt,
        "trust_server_certificate": trust_server_certificate,
        "extra_params": extra_params,
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
    form = _submitted_rule_form(
        name,
        data_source_id,
        sql_text,
        cron_expression,
        recipients,
        cc_recipients,
        subject_template,
        body_template,
        send_mode,
        query_timeout_seconds,
        max_rows,
        enabled,
    )
    source_id, parsed_send_mode, error_response = _validate_rule_form(
        request,
        admin,
        session,
        form=form,
        action="/rules",
        heading="新建规则",
    )
    if error_response is not None:
        return error_response

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


@router.post("/rules/validate-sql")
def validate_rule_sql(
    data_source_id: str = Form(""),
    sql_text: str = Form(""),
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    if not data_source_id:
        return JSONResponse(
            {"valid": False, "message": "请先选择数据源"},
            status_code=400,
        )

    try:
        validate_select_only_sql(sql_text)
    except SqlValidationError as exc:
        return JSONResponse(
            {"valid": False, "message": str(exc)},
            status_code=400,
        )

    try:
        source_id = int(data_source_id)
    except ValueError:
        return JSONResponse(
            {"valid": False, "message": "请选择有效的数据源"},
            status_code=400,
        )

    data_source = session.get(SqlDataSource, source_id)
    if data_source is None:
        return JSONResponse(
            {"valid": False, "message": "请选择有效的数据源"},
            status_code=400,
        )

    try:
        build_sql_client(data_source).validate_syntax(
            sql_text,
            timeout_seconds=data_source.connect_timeout_seconds,
        )
    except Exception as exc:
        return JSONResponse(
            {"valid": False, "message": f"SQL Server 语法检测失败：{exc}"},
            status_code=400,
        )

    return {"valid": True, "message": "SQL Server 语法检测通过"}


@router.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
def edit_rule_page(
    rule_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    rule = session.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return _template_response(
        request,
        "rule_form.html",
        _rule_form_context(
            request,
            admin,
            session,
            form=_rule_to_form(rule),
            action=f"/rules/{rule_id}",
            heading="编辑规则",
        ),
    )


@router.post("/rules/{rule_id}")
def update_rule(
    rule_id: int,
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
    rule = session.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")

    form = _submitted_rule_form(
        name,
        data_source_id,
        sql_text,
        cron_expression,
        recipients,
        cc_recipients,
        subject_template,
        body_template,
        send_mode,
        query_timeout_seconds,
        max_rows,
        enabled,
    )
    source_id, parsed_send_mode, error_response = _validate_rule_form(
        request,
        admin,
        session,
        form=form,
        action=f"/rules/{rule_id}",
        heading="编辑规则",
    )
    if error_response is not None:
        return error_response

    rule.name = name
    rule.data_source_id = source_id
    rule.sql_text = sql_text
    rule.cron_expression = cron_expression
    rule.recipients = recipients
    rule.cc_recipients = cc_recipients
    rule.subject_template = subject_template
    rule.body_template = body_template
    rule.send_mode = parsed_send_mode
    rule.query_timeout_seconds = query_timeout_seconds
    rule.max_rows = max_rows
    rule.enabled = _is_checked(enabled)
    rule.updated_at = utc_now()
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
    odbc_driver: str = Form("ODBC Driver 18 for SQL Server"),
    server_override: str = Form(""),
    encrypt: str = Form("yes"),
    trust_server_certificate: str = Form("yes"),
    extra_params: str = Form(""),
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
        odbc_driver=odbc_driver,
        server_override=server_override,
        encrypt=encrypt,
        trust_server_certificate=trust_server_certificate,
        extra_params=extra_params,
    )
    session.add(data_source)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.get("/settings/sql-server/{source_id}/edit", response_class=HTMLResponse)
def edit_sql_server_settings_page(
    source_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    data_source = session.get(SqlDataSource, source_id)
    if data_source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    return _template_response(
        request,
        "sql_server_form.html",
        {
            "admin": admin,
            "title": "编辑数据源",
            "form": _data_source_to_form(data_source),
            "action": f"/settings/sql-server/{source_id}",
            "error": "",
        },
    )


@router.post("/settings/sql-server/{source_id}/test")
def test_sql_server_settings(
    source_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    data_source = session.get(SqlDataSource, source_id)
    if data_source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")

    try:
        build_sql_client(data_source).query(
            "select 1 as ok",
            timeout_seconds=data_source.connect_timeout_seconds,
            max_rows=1,
        )
    except Exception as exc:
        return _template_response(
            request,
            "settings.html",
            _settings_context(request, admin, session, error=f"连接失败：{exc}"),
            status_code=400,
        )

    return _template_response(
        request,
        "settings.html",
        _settings_context(request, admin, session, notice=f"数据源 {data_source.name} 连接成功"),
    )


@router.post("/settings/sql-server/{source_id}")
def update_sql_server_settings(
    source_id: int,
    request: Request,
    name: str = Form(""),
    host: str = Form(""),
    port: int = Form(1433),
    database: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    enabled: str | None = Form(None),
    connect_timeout_seconds: int = Form(10),
    odbc_driver: str = Form("ODBC Driver 18 for SQL Server"),
    server_override: str = Form(""),
    encrypt: str = Form("yes"),
    trust_server_certificate: str = Form("yes"),
    extra_params: str = Form(""),
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    data_source = session.get(SqlDataSource, source_id)
    if data_source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")

    form = _submitted_data_source_form(
        name,
        host,
        port,
        database,
        username,
        password,
        enabled,
        connect_timeout_seconds,
        odbc_driver,
        server_override,
        encrypt,
        trust_server_certificate,
        extra_params,
    )
    existing = session.exec(select(SqlDataSource).where(SqlDataSource.name == name)).first()
    if existing is not None and existing.id != data_source.id:
        return _template_response(
            request,
            "sql_server_form.html",
            {
                "admin": admin,
                "title": "编辑数据源",
                "form": form,
                "action": f"/settings/sql-server/{source_id}",
                "error": "数据源名称已存在",
            },
            status_code=400,
        )

    data_source.name = name
    data_source.host = host
    data_source.port = port
    data_source.database = database
    data_source.username = username
    if password:
        data_source.encrypted_password = _cipher().encrypt(password)
    data_source.enabled = _is_checked(enabled)
    data_source.connect_timeout_seconds = connect_timeout_seconds
    data_source.odbc_driver = odbc_driver
    data_source.server_override = server_override
    data_source.encrypt = encrypt
    data_source.trust_server_certificate = trust_server_certificate
    data_source.extra_params = extra_params
    data_source.updated_at = utc_now()
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

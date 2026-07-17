import csv
import json
from io import StringIO
from urllib.parse import urlencode

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import require_admin
from app.crypto import SecretCipher
from app.dashboard import build_dashboard_context
from app.execution_lock import RuleExecutionInProgressError
from app.db import get_session
from app.execution_service import build_smtp_mailer, build_sql_client, execute_rule_by_id
from app.log_service import DEFAULT_PAGE_SIZE, LogFilters, list_execution_logs, list_mail_logs
from app.mailer import EmailMessage
from app.models import (
    AdminUser,
    AlertRule,
    AlertRuleVersion,
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    MailStatus,
    SendMode,
    SmtpConfig,
    SqlDataSource,
    TriggerType,
    utc_now,
)
from app.paths import TEMPLATES_DIR
from app.settings import get_settings
from app.sql_validator import SqlValidationError, validate_select_only_sql
from app.web_security import ensure_csrf_token, require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])
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
        {"request": request, "csrf_token": ensure_csrf_token(request), **context},
        status_code=status_code,
    )


def _csv_response(filename: str, headers: list[str], rows: list[list]) -> Response:
    stream = StringIO()
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    content = "\ufeff" + stream.getvalue()
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _is_checked(value: str | None) -> bool:
    return value in {"on", "true", "1"}


def _get_active_rule_or_404(session: Session, rule_id: int) -> AlertRule:
    rule = session.get(AlertRule, rule_id)
    if rule is None or rule.archived_at is not None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return rule


def _has_recipient_text(value: str) -> bool:
    return any(recipient.strip() for recipient in (value or "").replace(";", ",").split(","))


def _rule_snapshot(rule: AlertRule) -> dict:
    return {
        "id": rule.id,
        "name": rule.name,
        "data_source_id": rule.data_source_id,
        "sql_text": rule.sql_text,
        "cron_expression": rule.cron_expression,
        "recipients": rule.recipients,
        "cc_recipients": rule.cc_recipients,
        "subject_template": rule.subject_template,
        "body_template": rule.body_template,
        "send_mode": rule.send_mode.value,
        "query_timeout_seconds": rule.query_timeout_seconds,
        "max_rows": rule.max_rows,
        "enabled": rule.enabled,
        "notes": rule.notes,
        "dynamic_recipient_field": rule.dynamic_recipient_field,
        "dynamic_cc_field": rule.dynamic_cc_field,
        "suppress_duplicates": rule.suppress_duplicates,
        "suppression_key_field": rule.suppression_key_field,
        "suppression_window_hours": rule.suppression_window_hours,
        "created_at": rule.created_at.isoformat(),
        "updated_at": rule.updated_at.isoformat(),
    }


def _next_rule_version_number(session: Session, rule_id: int) -> int:
    latest_version = session.exec(
        select(AlertRuleVersion)
        .where(AlertRuleVersion.rule_id == rule_id)
        .order_by(AlertRuleVersion.version_number.desc())
    ).first()
    return 1 if latest_version is None else latest_version.version_number + 1


def _create_rule_version(session: Session, rule: AlertRule, admin: AdminUser) -> AlertRuleVersion:
    return AlertRuleVersion(
        rule_id=rule.id,
        version_number=_next_rule_version_number(session, rule.id),
        changed_by=admin.username,
        snapshot_json=json.dumps(_rule_snapshot(rule), ensure_ascii=False),
    )


def _version_snapshot(version: AlertRuleVersion) -> dict:
    try:
        snapshot = json.loads(version.snapshot_json)
    except json.JSONDecodeError:
        return {"name": "快照无法解析", "sql_text": ""}
    return snapshot if isinstance(snapshot, dict) else {"name": "快照无法解析", "sql_text": ""}


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
        "dynamic_recipient_field": rule.dynamic_recipient_field,
        "dynamic_cc_field": rule.dynamic_cc_field,
        "suppress_duplicates": "on" if rule.suppress_duplicates else "",
        "suppression_key_field": rule.suppression_key_field,
        "suppression_window_hours": str(rule.suppression_window_hours),
    }


def _rule_to_copy_form(rule: AlertRule) -> dict[str, str]:
    form = _rule_to_form(rule)
    form["name"] = f"{rule.name} 副本"
    form["enabled"] = "on"
    return form


def _rules_page_context(
    request: Request,
    admin: AdminUser,
    session: Session,
    *,
    error: str = "",
) -> dict:
    rules = session.exec(
        select(AlertRule)
        .where(AlertRule.archived_at.is_(None))
        .order_by(AlertRule.created_at.desc())
    ).all()
    imported = request.query_params.get("imported", "")
    notice = f"成功导入 {imported} 条规则" if imported.isdigit() else ""
    return {
        "request": request,
        "admin": admin,
        "title": "预警规则",
        "rules": rules,
        "error": error,
        "notice": notice,
    }


def _rule_export_payload(session: Session) -> dict:
    rules = session.exec(
        select(AlertRule)
        .where(AlertRule.archived_at.is_(None))
        .order_by(AlertRule.created_at.desc())
    ).all()
    data_sources = {source.id: source.name for source in session.exec(select(SqlDataSource)).all()}
    return {
        "version": 1,
        "exported_at": utc_now().isoformat(),
        "rules": [
            {
                "name": rule.name,
                "data_source_name": data_sources.get(rule.data_source_id, ""),
                "sql_text": rule.sql_text,
                "cron_expression": rule.cron_expression,
                "recipients": rule.recipients,
                "cc_recipients": rule.cc_recipients,
                "subject_template": rule.subject_template,
                "body_template": rule.body_template,
                "send_mode": rule.send_mode.value,
                "query_timeout_seconds": rule.query_timeout_seconds,
                "max_rows": rule.max_rows,
                "enabled": rule.enabled,
                "notes": rule.notes,
                "dynamic_recipient_field": rule.dynamic_recipient_field,
                "dynamic_cc_field": rule.dynamic_cc_field,
                "suppress_duplicates": rule.suppress_duplicates,
                "suppression_key_field": rule.suppression_key_field,
                "suppression_window_hours": rule.suppression_window_hours,
            }
            for rule in rules
        ],
    }


def _required_import_text(rule_data: dict, field: str, index: int, label: str) -> str:
    value = rule_data.get(field, "")
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        raise ValueError(f"第 {index} 条规则缺少{label}")
    return value


def _optional_import_text(rule_data: dict, field: str) -> str:
    value = rule_data.get(field, "")
    return value if isinstance(value, str) else str(value)


def _positive_import_int(rule_data: dict, field: str, index: int, label: str, default: int) -> int:
    value = rule_data.get(field, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"第 {index} 条规则{label}必须是正整数") from exc
    if parsed <= 0:
        raise ValueError(f"第 {index} 条规则{label}必须是正整数")
    return parsed


def _build_imported_rules(payload: dict, session: Session) -> list[AlertRule]:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("导入文件格式无效：仅支持 version 1")

    rules_data = payload.get("rules")
    if not isinstance(rules_data, list):
        raise ValueError("导入文件格式无效：rules 必须是列表")

    sources_by_name = {source.name: source for source in session.exec(select(SqlDataSource)).all()}
    imported_rules = []
    for index, rule_data in enumerate(rules_data, start=1):
        if not isinstance(rule_data, dict):
            raise ValueError(f"第 {index} 条规则格式无效")

        name = _required_import_text(rule_data, "name", index, "规则名称")
        data_source_name = _required_import_text(rule_data, "data_source_name", index, "数据源名称")
        data_source = sources_by_name.get(data_source_name)
        if data_source is None:
            raise ValueError(f"第 {index} 条规则的数据源不存在")

        sql_text = _required_import_text(rule_data, "sql_text", index, "SQL 查询")
        try:
            validate_select_only_sql(sql_text)
        except SqlValidationError as exc:
            raise ValueError(f"第 {index} 条规则 SQL 无效：{exc}") from exc

        cron_expression = _required_import_text(rule_data, "cron_expression", index, "Cron 表达式")
        try:
            CronTrigger.from_crontab(cron_expression)
        except ValueError as exc:
            raise ValueError(f"第 {index} 条规则 Cron 表达式无效") from exc

        send_mode_value = _optional_import_text(rule_data, "send_mode") or SendMode.SUMMARY.value
        try:
            send_mode = SendMode(send_mode_value)
        except ValueError as exc:
            raise ValueError(f"第 {index} 条规则发送方式无效") from exc
        dynamic_recipient_field = _optional_import_text(rule_data, "dynamic_recipient_field").strip()
        if dynamic_recipient_field and send_mode != SendMode.PER_ROW:
            raise ValueError(f"第 {index} 条规则动态收件人字段仅支持每行一封模式")
        recipients = _optional_import_text(rule_data, "recipients").strip()
        if not _has_recipient_text(recipients) and not dynamic_recipient_field:
            raise ValueError(f"第 {index} 条规则缺少收件人或动态收件人字段")

        imported_rules.append(
            AlertRule(
                name=name,
                data_source_id=data_source.id,
                sql_text=sql_text,
                cron_expression=cron_expression,
                recipients=recipients,
                cc_recipients=_optional_import_text(rule_data, "cc_recipients"),
                subject_template=_optional_import_text(rule_data, "subject_template"),
                body_template=_optional_import_text(rule_data, "body_template"),
                send_mode=send_mode,
                query_timeout_seconds=_positive_import_int(
                    rule_data,
                    "query_timeout_seconds",
                    index,
                    "查询超时时间",
                    30,
                ),
                max_rows=_positive_import_int(rule_data, "max_rows", index, "最大返回行数", 500),
                enabled=bool(rule_data.get("enabled", True)),
                notes=_optional_import_text(rule_data, "notes"),
                dynamic_recipient_field=dynamic_recipient_field,
                dynamic_cc_field=_optional_import_text(rule_data, "dynamic_cc_field").strip(),
                suppress_duplicates=bool(rule_data.get("suppress_duplicates", False)),
                suppression_key_field=_optional_import_text(rule_data, "suppression_key_field").strip(),
                suppression_window_hours=_positive_import_int(
                    rule_data,
                    "suppression_window_hours",
                    index,
                    "重复抑制窗口",
                    24,
                ),
            )
        )
        if imported_rules[-1].suppress_duplicates and not imported_rules[-1].suppression_key_field:
            raise ValueError(f"第 {index} 条规则启用重复抑制时必须填写去重字段")
    return imported_rules


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
    dynamic_recipient_field: str,
    dynamic_cc_field: str,
    suppress_duplicates: str | None,
    suppression_key_field: str,
    suppression_window_hours: int,
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
        "dynamic_recipient_field": dynamic_recipient_field.strip(),
        "dynamic_cc_field": dynamic_cc_field.strip(),
        "suppress_duplicates": "on" if _is_checked(suppress_duplicates) else "",
        "suppression_key_field": suppression_key_field.strip(),
        "suppression_window_hours": str(suppression_window_hours),
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

    if form.get("dynamic_recipient_field", "").strip() and parsed_send_mode != SendMode.PER_ROW:
        return None, None, _template_response(
            request,
            "rule_form.html",
            _rule_form_context(
                request,
                admin,
                session,
                error="动态收件人字段仅支持每行一封模式",
                form=form,
                action=action,
                heading=heading,
            ),
            status_code=400,
        )

    if not _has_recipient_text(form.get("recipients", "")) and not form.get(
        "dynamic_recipient_field", ""
    ).strip():
        return None, None, _template_response(
            request,
            "rule_form.html",
            _rule_form_context(
                request,
                admin,
                session,
                error="请填写收件人或动态收件人字段",
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

    if form.get("suppress_duplicates"):
        if not form.get("suppression_key_field", "").strip():
            return None, None, _template_response(
                request,
                "rule_form.html",
                _rule_form_context(
                    request,
                    admin,
                    session,
                    error="启用重复抑制时必须填写去重字段",
                    form=form,
                    action=action,
                    heading=heading,
                ),
                status_code=400,
            )
        try:
            suppression_window_hours = int(form.get("suppression_window_hours", "24"))
        except ValueError:
            suppression_window_hours = 0
        if suppression_window_hours <= 0:
            return None, None, _template_response(
                request,
                "rule_form.html",
                _rule_form_context(
                    request,
                    admin,
                    session,
                    error="重复抑制窗口必须是正整数",
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


def _smtp_config_to_form(smtp_config: SmtpConfig) -> dict[str, str]:
    return {
        "host": smtp_config.host,
        "port": str(smtp_config.port),
        "username": smtp_config.username,
        "password": "",
        "sender": smtp_config.sender,
        "use_tls": "on" if smtp_config.use_tls else "",
        "use_ssl": "on" if smtp_config.use_ssl else "",
        "enabled": "on" if smtp_config.enabled else "",
        "timeout_seconds": str(smtp_config.timeout_seconds),
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
def dashboard(
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _template_response(
        request,
        "dashboard.html",
        {
            "admin": admin,
            "title": "仪表盘",
            **build_dashboard_context(session),
        },
    )


@router.get("/rules", response_class=HTMLResponse)
def rules_page(
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    return _template_response(
        request,
        "rules.html",
        _rules_page_context(request, admin, session),
    )


@router.get("/rules/export.json")
def export_rules_json(
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    return JSONResponse(
        _rule_export_payload(session),
        headers={"Content-Disposition": 'attachment; filename="alert-rules.json"'},
    )


@router.post("/rules/import")
async def import_rules_json(
    request: Request,
    file: UploadFile = File(...),
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    try:
        payload = json.loads((await file.read()).decode("utf-8"))
        imported_rules = _build_imported_rules(payload, session)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _template_response(
            request,
            "rules.html",
            _rules_page_context(request, admin, session, error="导入文件必须是有效的 JSON"),
            status_code=400,
        )
    except ValueError as exc:
        return _template_response(
            request,
            "rules.html",
            _rules_page_context(request, admin, session, error=str(exc)),
            status_code=400,
        )

    for rule in imported_rules:
        session.add(rule)
    session.commit()
    return RedirectResponse(f"/rules?imported={len(imported_rules)}", status_code=303)


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
    dynamic_recipient_field: str = Form(""),
    dynamic_cc_field: str = Form(""),
    suppress_duplicates: str | None = Form(None),
    suppression_key_field: str = Form(""),
    suppression_window_hours: int = Form(24),
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
        dynamic_recipient_field,
        dynamic_cc_field,
        suppress_duplicates,
        suppression_key_field,
        suppression_window_hours,
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
        dynamic_recipient_field=dynamic_recipient_field.strip(),
        dynamic_cc_field=dynamic_cc_field.strip(),
        suppress_duplicates=_is_checked(suppress_duplicates),
        suppression_key_field=suppression_key_field.strip(),
        suppression_window_hours=suppression_window_hours,
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


@router.post("/rules/preview-sql")
def preview_rule_sql(
    data_source_id: str = Form(""),
    sql_text: str = Form(""),
    query_timeout_seconds: int = Form(30),
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    if not data_source_id:
        return JSONResponse(
            {"success": False, "message": "请先选择数据源"},
            status_code=400,
        )

    try:
        validate_select_only_sql(sql_text)
    except SqlValidationError as exc:
        return JSONResponse(
            {"success": False, "message": str(exc)},
            status_code=400,
        )

    try:
        source_id = int(data_source_id)
    except ValueError:
        return JSONResponse(
            {"success": False, "message": "请选择有效的数据源"},
            status_code=400,
        )

    data_source = session.get(SqlDataSource, source_id)
    if data_source is None:
        return JSONResponse(
            {"success": False, "message": "请选择有效的数据源"},
            status_code=400,
        )

    try:
        preview = build_sql_client(data_source).query(
            sql_text,
            timeout_seconds=query_timeout_seconds,
            max_rows=5,
        )
    except Exception as exc:
        return JSONResponse(
            {"success": False, "message": f"预览失败：{exc}"},
            status_code=400,
        )

    rows = preview.rows
    columns = list(rows[0].keys()) if rows else []
    message = f"查询成功，返回 {len(rows)} 行预览结果" if rows else "查询成功，暂无结果"
    return {"success": True, "message": message, "columns": columns, "rows": rows}


@router.get("/rules/{rule_id}/edit", response_class=HTMLResponse)
def edit_rule_page(
    rule_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    rule = _get_active_rule_or_404(session, rule_id)
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


@router.get("/rules/{rule_id}/versions", response_class=HTMLResponse)
def rule_versions_page(
    rule_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    rule = _get_active_rule_or_404(session, rule_id)
    versions = session.exec(
        select(AlertRuleVersion)
        .where(AlertRuleVersion.rule_id == rule_id)
        .order_by(AlertRuleVersion.version_number.desc())
    ).all()
    return _template_response(
        request,
        "rule_versions.html",
        {
            "admin": admin,
            "title": "规则版本历史",
            "rule": rule,
            "version_rows": [
                {
                    "version": version,
                    "snapshot": _version_snapshot(version),
                }
                for version in versions
            ],
        },
    )


@router.get("/rules/{rule_id}/copy", response_class=HTMLResponse)
def copy_rule_page(
    rule_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    rule = _get_active_rule_or_404(session, rule_id)
    return _template_response(
        request,
        "rule_form.html",
        _rule_form_context(
            request,
            admin,
            session,
            form=_rule_to_copy_form(rule),
            action="/rules",
            heading="复制规则",
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
    dynamic_recipient_field: str = Form(""),
    dynamic_cc_field: str = Form(""),
    suppress_duplicates: str | None = Form(None),
    suppression_key_field: str = Form(""),
    suppression_window_hours: int = Form(24),
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    rule = _get_active_rule_or_404(session, rule_id)

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
        dynamic_recipient_field,
        dynamic_cc_field,
        suppress_duplicates,
        suppression_key_field,
        suppression_window_hours,
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

    session.add(_create_rule_version(session, rule, admin))
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
    rule.dynamic_recipient_field = dynamic_recipient_field.strip()
    rule.dynamic_cc_field = dynamic_cc_field.strip()
    rule.suppress_duplicates = _is_checked(suppress_duplicates)
    rule.suppression_key_field = suppression_key_field.strip()
    rule.suppression_window_hours = suppression_window_hours
    rule.updated_at = utc_now()
    session.add(rule)
    session.commit()
    return RedirectResponse("/rules", status_code=303)


@router.post("/rules/{rule_id}/delete")
def archive_rule(
    rule_id: int,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    rule = _get_active_rule_or_404(session, rule_id)
    now = utc_now()
    rule.enabled = False
    rule.archived_at = now
    rule.updated_at = now
    session.add(rule)
    session.commit()
    return RedirectResponse("/rules", status_code=303)


@router.post("/rules/{rule_id}/run")
def run_rule(
    request: Request,
    rule_id: int,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _get_active_rule_or_404(session, rule_id)
    try:
        execute_rule_by_id(session, rule_id, trigger_type=TriggerType.MANUAL)
    except RuleExecutionInProgressError:
        return _template_response(
            request,
            "rules.html",
            _rules_page_context(
                request,
                admin,
                session,
                error="规则正在执行，请稍后重试",
            ),
            status_code=409,
        )
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


@router.post("/settings/sql-server/{source_id}/delete")
def delete_sql_server_settings(
    source_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    data_source = session.get(SqlDataSource, source_id)
    if data_source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")

    rules = session.exec(
        select(AlertRule)
        .where(AlertRule.data_source_id == source_id)
        .order_by(AlertRule.name)
    ).all()
    if rules:
        rule_names = "、".join(rule.name for rule in rules)
        return _template_response(
            request,
            "settings.html",
            _settings_context(
                request,
                admin,
                session,
                error=f"数据源正在被规则引用：{rule_names}",
            ),
            status_code=400,
        )

    session.delete(data_source)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


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


@router.get("/settings/smtp/{config_id}/edit", response_class=HTMLResponse)
def edit_smtp_settings_page(
    config_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    smtp_config = session.get(SmtpConfig, config_id)
    if smtp_config is None:
        raise HTTPException(status_code=404, detail="SMTP 配置不存在")
    return _template_response(
        request,
        "smtp_form.html",
        {
            "admin": admin,
            "title": "编辑 SMTP 配置",
            "form": _smtp_config_to_form(smtp_config),
            "action": f"/settings/smtp/{config_id}",
            "error": "",
        },
    )


@router.post("/settings/smtp/{config_id}")
def update_smtp_settings(
    config_id: int,
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
    smtp_config = session.get(SmtpConfig, config_id)
    if smtp_config is None:
        raise HTTPException(status_code=404, detail="SMTP 配置不存在")

    smtp_config.host = host
    smtp_config.port = port
    smtp_config.username = username
    if password:
        smtp_config.encrypted_password = _cipher().encrypt(password)
    smtp_config.sender = sender
    smtp_config.use_tls = _is_checked(use_tls)
    smtp_config.use_ssl = _is_checked(use_ssl)
    smtp_config.enabled = _is_checked(enabled)
    smtp_config.timeout_seconds = timeout_seconds
    smtp_config.updated_at = utc_now()
    session.add(smtp_config)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/smtp/{config_id}/delete")
def delete_smtp_settings(
    config_id: int,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    smtp_config = session.get(SmtpConfig, config_id)
    if smtp_config is None:
        raise HTTPException(status_code=404, detail="SMTP 配置不存在")

    session.delete(smtp_config)
    session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/smtp/{config_id}/test")
def test_smtp_settings(
    config_id: int,
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    smtp_config = session.get(SmtpConfig, config_id)
    if smtp_config is None:
        raise HTTPException(status_code=404, detail="SMTP 配置不存在")

    message = EmailMessage(
        recipients=[smtp_config.sender],
        cc_recipients=[],
        subject="SQL 预警系统 SMTP 测试",
        html_body="<p>SMTP 配置已可用。</p>",
    )
    result = build_smtp_mailer(smtp_config).send(message)
    if not result.success:
        return _template_response(
            request,
            "settings.html",
            _settings_context(
                request,
                admin,
                session,
                error=f"SMTP 测试发送失败：{result.error_message or '未知错误'}",
            ),
            status_code=400,
        )

    return _template_response(
        request,
        "settings.html",
        _settings_context(request, admin, session, notice="SMTP 测试邮件发送成功"),
    )


@router.get("/logs", response_class=HTMLResponse)
def logs_page(
    request: Request,
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    filters = LogFilters(
        execution_status=request.query_params.get("execution_status", "").strip(),
        trigger_type=request.query_params.get("trigger_type", "").strip(),
        rule_id=request.query_params.get("rule_id", "").strip(),
        mail_status=request.query_params.get("mail_status", "").strip(),
        keyword=request.query_params.get("keyword", "").strip(),
    )
    execution_page = list_execution_logs(
        session,
        filters,
        page=_log_page_parameter(request, "execution_page", 1),
        page_size=_log_page_parameter(request, "page_size", DEFAULT_PAGE_SIZE),
    )
    mail_page = list_mail_logs(
        session,
        filters,
        page=_log_page_parameter(request, "mail_page", 1),
        page_size=execution_page.page_size,
    )

    def log_page_url(execution_page_number: int, mail_page_number: int) -> str:
        return "/logs?" + urlencode(
            {
                "execution_status": filters.execution_status,
                "trigger_type": filters.trigger_type,
                "rule_id": filters.rule_id,
                "mail_status": filters.mail_status,
                "keyword": filters.keyword,
                "execution_page": execution_page_number,
                "mail_page": mail_page_number,
                "page_size": execution_page.page_size,
            }
        )

    return _template_response(
        request,
        "logs.html",
        {
            "admin": admin,
            "title": "日志",
            "filters": filters,
            "execution_statuses": list(ExecutionStatus),
            "trigger_types": list(TriggerType),
            "mail_statuses": list(MailStatus),
            "execution_page": execution_page,
            "mail_page": mail_page,
            "log_page_url": log_page_url,
        },
    )


def _log_page_parameter(request: Request, name: str, default: int) -> int:
    try:
        return int(request.query_params.get(name, default))
    except (TypeError, ValueError):
        return default


@router.get("/logs/executions.csv")
def export_execution_logs_csv(
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    execution_logs = session.exec(select(ExecutionLog).order_by(ExecutionLog.started_at.desc())).all()
    return _csv_response(
        "execution-logs.csv",
        [
            "ID",
            "规则ID",
            "触发方式",
            "状态",
            "开始时间",
            "结束时间",
            "返回行数",
            "邮件数",
            "耗时毫秒",
            "错误类型",
            "错误信息",
        ],
        [
            [
                log.id,
                log.rule_id,
                log.trigger_type,
                log.status,
                log.started_at,
                log.finished_at or "",
                log.row_count,
                log.email_count,
                log.duration_ms,
                log.error_type,
                log.error_message,
            ]
            for log in execution_logs
        ],
    )


@router.get("/logs/mails.csv")
def export_mail_logs_csv(
    admin: AdminUser = Depends(require_admin),
    session: Session = Depends(get_session),
):
    _ = admin
    mail_logs = session.exec(select(MailLog).order_by(MailLog.sent_at.desc())).all()
    return _csv_response(
        "mail-logs.csv",
        ["ID", "执行记录ID", "收件人", "抄送", "主题", "状态", "错误信息", "发送时间"],
        [
            [
                log.id,
                log.execution_log_id,
                log.recipients,
                log.cc_recipients,
                log.subject,
                log.status,
                log.error_message,
                log.sent_at,
            ]
            for log in mail_logs
        ],
    )

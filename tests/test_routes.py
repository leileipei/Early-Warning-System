import importlib
import json
import re
from asyncio import run
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import Session, select

from app.models import (
    AdminUser,
    AlertRule,
    AlertRuleVersion,
    AlertSuppression,
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
from app.mailer import MailSendResult
from app.settings import Settings
from app.sql_client import QueryResult
from app.web_security import require_csrf

VALID_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
CSRF_PATTERN = re.compile(r'name="_csrf_token" value="([^"]+)"')
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "x-frame-options": "DENY",
}
CONTENT_SECURITY_POLICY_DIRECTIVES = frozenset(
    {
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    }
)


def _assert_security_headers(response, *, hsts: bool = False):
    for header, value in SECURITY_HEADERS.items():
        assert response.headers[header] == value
    csp_directives = frozenset(
        directive.strip()
        for directive in response.headers["content-security-policy"].split(";")
        if directive.strip()
    )
    assert csp_directives == CONTENT_SECURITY_POLICY_DIRECTIVES
    assert "'unsafe-inline'" not in csp_directives
    if hsts:
        assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
    else:
        assert "strict-transport-security" not in response.headers


def _set_required_settings(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-with-32-bytes")
    monkeypatch.setenv("SECRET_KEY", VALID_FERNET_KEY)


def _load_create_app():
    from app.settings import get_settings

    get_settings.cache_clear()
    main = importlib.import_module("app.main")
    return main.create_app, get_settings


def _admin_user():
    return AdminUser(id=1, username="admin", password_hash="hash")


def _client_with_admin(monkeypatch, session):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    routes = importlib.import_module("app.routes")
    app = create_app()

    def override_session():
        yield session

    app.dependency_overrides[routes.require_admin] = _admin_user
    app.dependency_overrides[routes.get_session] = override_session
    app.dependency_overrides[require_csrf] = lambda: None
    return TestClient(app), get_settings, app


async def _read_stream_body(response: StreamingResponse) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


def _csrf_token(client: TestClient) -> str:
    response = client.get("/login")
    match = CSRF_PATTERN.search(response.text)
    assert match is not None
    return match.group(1)


def _post_as_unauthenticated(client: TestClient, path: str, *, data=None, files=None, **kwargs):
    payload = dict(data or {})
    payload["_csrf_token"] = _csrf_token(client)
    return client.post(path, data=payload, files=files, **kwargs)


def _create_data_source(session):
    data_source = SqlDataSource(
        name="生产库",
        host="db.example.com",
        port=1433,
        database="erp",
        username="readonly",
        encrypted_password="encrypted",
        enabled=True,
    )
    session.add(data_source)
    session.commit()
    session.refresh(data_source)
    return data_source


def _create_smtp_config(session, *, enabled=True):
    smtp_config = SmtpConfig(
        host="smtp.example.com",
        port=587,
        username="mailer",
        encrypted_password="encrypted",
        sender="alerts@example.com",
        enabled=enabled,
    )
    session.add(smtp_config)
    session.commit()
    session.refresh(smtp_config)
    return smtp_config


def _create_rule(session, data_source, **overrides):
    data = {
        "name": "大额订单",
        "data_source_id": data_source.id,
        "sql_text": "select id, amount from orders",
        "cron_expression": "0 9 * * *",
        "recipients": "ops@example.com",
        "cc_recipients": "",
        "subject_template": "大额订单预警",
        "body_template": "{{table}}",
        "send_mode": SendMode.SUMMARY,
        "enabled": True,
    }
    data.update(overrides)
    rule = AlertRule(**data)
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def _valid_rule_form(data_source_id):
    return {
        "name": "大额订单",
        "data_source_id": str(data_source_id),
        "sql_text": "select id, amount from orders",
        "cron_expression": "0 9 * * *",
        "recipients": "ops@example.com",
        "cc_recipients": "",
        "subject_template": "大额订单预警",
        "body_template": "{{table}}",
        "send_mode": "summary",
        "query_timeout_seconds": "30",
        "max_rows": "500",
        "enabled": "on",
    }


def test_health_endpoint_returns_ok(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    finally:
        get_settings.cache_clear()


def test_app_title_uses_configured_app_name(monkeypatch):
    _set_required_settings(monkeypatch)
    monkeypatch.setenv("APP_NAME", "测试预警系统")
    create_app, get_settings = _load_create_app()

    get_settings.cache_clear()
    try:
        app = create_app()

        assert app.title == "测试预警系统"
    finally:
        get_settings.cache_clear()


def test_navigation_marks_the_current_page(monkeypatch, session):
    client, get_settings, _ = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules")

        assert response.status_code == 200
        assert 'class="nav-link is-active" href="/rules" aria-current="page"' in response.text
        assert 'class="nav-link" href="/logs">日志</a>' in response.text
    finally:
        get_settings.cache_clear()


def test_settings_require_secret_values(tmp_path, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    from app.settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    error_fields = {error["loc"][0] for error in exc_info.value.errors()}
    assert {"session_secret", "secret_key"} <= error_fields


@pytest.mark.parametrize(
    ("field_name", "settings_values"),
    [
        (
            "session_secret",
            {
                "session_secret": "REPLACE_ME_WITH_RANDOM_SESSION_SECRET",
                "secret_key": VALID_FERNET_KEY,
            },
        ),
        (
            "secret_key",
            {
                "session_secret": "valid-session-secret",
                "secret_key": "REPLACE_ME_WITH_32_BYTE_URL_SAFE_FERNET_KEY",
            },
        ),
    ],
)
def test_settings_reject_replace_me_secret_placeholders(field_name, settings_values):
    from app.settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(**settings_values)

    error_fields = {error["loc"][0] for error in exc_info.value.errors()}
    assert field_name in error_fields


def test_settings_reject_invalid_secret_key():
    from app.settings import Settings

    with pytest.raises(ValidationError) as exc_info:
        Settings(session_secret="valid-session-secret-with-32-bytes", secret_key="not-a-fernet-key")

    error_fields = {error["loc"][0] for error in exc_info.value.errors()}
    assert "secret_key" in error_fields


def test_scheduler_sync_interval_defaults_to_ten_seconds():
    from app.settings import Settings

    settings = Settings(
        session_secret="valid-session-secret-with-32-bytes",
        secret_key=VALID_FERNET_KEY,
    )

    assert settings.scheduler_sync_interval_seconds == 10.0


def test_rule_execution_lease_defaults_to_two_hours():
    settings = Settings(session_secret="valid-session-secret-with-32-bytes", secret_key=VALID_FERNET_KEY)

    assert settings.rule_execution_lease_seconds == 7200


@pytest.mark.parametrize("value", [0, -1])
def test_rule_execution_lease_rejects_non_positive_values(value):
    with pytest.raises(ValidationError):
        Settings(
            session_secret="valid-session-secret-with-32-bytes",
            secret_key=VALID_FERNET_KEY,
            rule_execution_lease_seconds=value,
        )


def test_web_security_settings_have_safe_compatible_defaults():
    from app.settings import Settings

    settings = Settings(session_secret="valid-session-secret-with-32-bytes", secret_key=VALID_FERNET_KEY)

    assert settings.session_cookie_secure is False
    assert settings.login_max_failures == 5
    assert settings.login_failure_window_seconds == 900
    assert settings.login_lockout_seconds == 900


@pytest.mark.parametrize(
    "field_name",
    ["login_max_failures", "login_failure_window_seconds", "login_lockout_seconds"],
)
@pytest.mark.parametrize("invalid_value", [0, -1])
def test_web_security_integer_settings_reject_non_positive_values(field_name, invalid_value):
    from app.settings import Settings

    values = {
        "session_secret": "valid-session-secret-with-32-bytes",
        "secret_key": VALID_FERNET_KEY,
        field_name: invalid_value,
    }

    with pytest.raises(ValidationError) as exc_info:
        Settings(**values)

    assert any(error["loc"] == (field_name,) for error in exc_info.value.errors())


@pytest.mark.parametrize(
    ("secure_value", "expects_secure"),
    [("false", False), ("true", True)],
)
def test_session_cookie_security_flags_follow_configuration(monkeypatch, secure_value, expects_secure):
    from fastapi import Request

    _set_required_settings(monkeypatch)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", secure_value)
    create_app, get_settings = _load_create_app()
    app = create_app()

    @app.get("/session-cookie-test")
    def session_cookie_test(request: Request):
        request.session["probe"] = "value"
        return {"ok": True}

    try:
        response = TestClient(app).get("/session-cookie-test")
        cookie = response.headers["set-cookie"].lower()

        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert ("secure" in cookie) is expects_secure
    finally:
        get_settings.cache_clear()


def test_scheduler_sync_interval_reads_environment(monkeypatch):
    from app.settings import Settings

    monkeypatch.setenv("SCHEDULER_SYNC_INTERVAL_SECONDS", "2.5")

    settings = Settings(
        session_secret="valid-session-secret-with-32-bytes",
        secret_key=VALID_FERNET_KEY,
    )

    assert settings.scheduler_sync_interval_seconds == 2.5


@pytest.mark.parametrize("value", ["0", "-1", "inf", "-inf", "nan"])
def test_scheduler_sync_interval_rejects_non_finite_or_non_positive_values(value, monkeypatch):
    from pydantic import ValidationError
    from app.settings import Settings

    monkeypatch.setenv("SCHEDULER_SYNC_INTERVAL_SECONDS", value)

    with pytest.raises(ValidationError) as exc_info:
        Settings(
            session_secret="valid-session-secret-with-32-bytes",
            secret_key=VALID_FERNET_KEY,
        )

    assert any(error["loc"] == ("scheduler_sync_interval_seconds",) for error in exc_info.value.errors())


def test_settings_reads_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    tmp_path.joinpath(".env").write_text(
        "\n".join(
            [
                "APP_NAME=Env 文件预警系统",
                "SESSION_SECRET=dotenv-session-secret-with-32-bytes",
                f"SECRET_KEY={VALID_FERNET_KEY}",
            ]
        ),
        encoding="utf-8",
    )

    from app.settings import get_settings

    get_settings.cache_clear()
    try:
        settings = get_settings()

        assert settings.app_name == "Env 文件预警系统"
        assert settings.session_secret == "dotenv-session-secret-with-32-bytes"
        assert settings.secret_key == VALID_FERNET_KEY
    finally:
        get_settings.cache_clear()


def test_login_page_renders(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/login")

        assert response.status_code == 200
        assert "用户名" in response.text
    finally:
        get_settings.cache_clear()


def test_login_page_renders_csrf_hidden_field(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        response = TestClient(create_app()).get("/login")

        assert response.status_code == 200
        assert CSRF_PATTERN.search(response.text)
    finally:
        get_settings.cache_clear()


def test_security_headers_cover_login_redirect_and_not_found_responses(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        login_response = client.get("/login")
        redirect_response = client.get(
            "/",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )
        not_found_response = client.get("/does-not-exist")

        for response in (login_response, redirect_response, not_found_response):
            _assert_security_headers(response)
    finally:
        get_settings.cache_clear()


def test_security_headers_cover_admin_and_unhandled_error_responses(monkeypatch, session):
    client, get_settings, app = _client_with_admin(monkeypatch, session)

    @app.get("/security-header-error")
    def security_header_error():
        raise RuntimeError("expected test error")

    try:
        admin_response = client.get("/rules")
        error_response = TestClient(app, raise_server_exceptions=False).get("/security-header-error")

        assert error_response.status_code == 500
        _assert_security_headers(admin_response)
        _assert_security_headers(error_response)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("secure_value", "expects_hsts"),
    [("false", False), ("true", True)],
)
def test_hsts_follows_session_cookie_secure_configuration(monkeypatch, secure_value, expects_hsts):
    _set_required_settings(monkeypatch)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", secure_value)
    create_app, get_settings = _load_create_app()
    try:
        response = TestClient(create_app()).get("/login")

        _assert_security_headers(response, hsts=expects_hsts)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("path", "minimum_form_count"),
    [("/rules", 2), ("/rules/new", 2), ("/settings", 3)],
)
def test_admin_pages_render_csrf_for_every_post_form(monkeypatch, session, path, minimum_form_count):
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get(path)

        assert response.status_code == 200
        assert response.text.count('name="_csrf_token"') >= minimum_form_count
        assert response.text.count('name="_csrf_token"') == response.text.count('method="post"')
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_sql_ajax_requests_include_csrf_token():
    script = Path("app/static/app.js").read_text(encoding="utf-8")

    assert script.count('payload.append("_csrf_token"') == 2


def test_templates_do_not_include_inline_script_or_event_attributes():
    for template_path in Path("app/templates").glob("*.html"):
        template = template_path.read_text(encoding="utf-8")

        script_contents = re.findall(r"<script\b[^>]*>(.*?)</script>", template, flags=re.DOTALL | re.I)
        assert all(not script.strip() for script in script_contents), template_path
        assert re.search(r"\son[a-z]+\s*=", template, flags=re.I) is None, template_path


def test_dashboard_redirects_browser_to_login_when_unauthenticated(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get(
            "/",
            headers={"accept": "text/html"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/login"
    finally:
        get_settings.cache_clear()


def test_dashboard_uses_real_metrics(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source, name="实时规则")
    session.add(
        ExecutionLog(
            rule_id=rule.id,
            trigger_type=TriggerType.MANUAL,
            status=ExecutionStatus.FAILED,
            started_at=utc_now(),
            row_count=3,
            email_count=1,
        )
    )
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/")

        assert response.status_code == 200
        assert 'data-testid="enabled-rule-count">1</strong>' in response.text
        assert 'data-testid="recent-failure-count">1</strong>' in response.text
        assert "实时规则" in response.text
        assert "暂无执行记录" not in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_rules_page_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/rules")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_settings_page_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/settings")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_create_rule_requires_admin_session(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, "/rules", data={"name": "x"})

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("csrf_token", [None, "wrong-token"])
def test_create_rule_enforces_real_page_router_csrf(monkeypatch, session, csrf_token):
    data_source = _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        app.dependency_overrides.pop(require_csrf)
        _csrf_token(client)
        form_data = _valid_rule_form(data_source.id)
        if csrf_token is not None:
            form_data["_csrf_token"] = csrf_token

        response = client.post("/rules", data=form_data, follow_redirects=False)

        assert response.status_code == 403
        assert response.json() == {"detail": "请求安全校验失败"}
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_persists_alert_rule(monkeypatch, session):
    data_source = _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules",
            data=_valid_rule_form(data_source.id),
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/rules"
        rule = session.exec(select(AlertRule)).one()
        assert rule.name == "大额订单"
        assert rule.data_source_id == data_source.id
        assert rule.sql_text == "select id, amount from orders"
        assert rule.enabled is True
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_persists_duplicate_suppression_settings(monkeypatch, session):
    data_source = _create_data_source(session)
    form_data = _valid_rule_form(data_source.id)
    form_data.update(
        {
            "suppress_duplicates": "on",
            "suppression_key_field": "order_id",
            "suppression_window_hours": "12",
        }
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data, follow_redirects=False)

        assert response.status_code == 303
        rule = session.exec(select(AlertRule)).one()
        assert rule.suppress_duplicates is True
        assert rule.suppression_key_field == "order_id"
        assert rule.suppression_window_hours == 12
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_persists_dynamic_recipient_fields(monkeypatch, session):
    data_source = _create_data_source(session)
    form_data = _valid_rule_form(data_source.id)
    form_data.update(
        {
            "send_mode": "per_row",
            "dynamic_recipient_field": "owner_email",
            "dynamic_cc_field": "manager_email",
        }
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data, follow_redirects=False)

        assert response.status_code == 303
        rule = session.exec(select(AlertRule)).one()
        assert rule.dynamic_recipient_field == "owner_email"
        assert rule.dynamic_cc_field == "manager_email"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_rejects_dynamic_recipient_field_for_summary_mode(monkeypatch, session):
    data_source = _create_data_source(session)
    form_data = _valid_rule_form(data_source.id)
    form_data.update({"send_mode": "summary", "dynamic_recipient_field": "owner_email"})
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data)

        assert response.status_code == 400
        assert "动态收件人字段仅支持每行一封模式" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_allows_dynamic_recipient_without_fixed_recipients(monkeypatch, session):
    data_source = _create_data_source(session)
    form_data = _valid_rule_form(data_source.id)
    form_data.update(
        {
            "send_mode": "per_row",
            "recipients": "",
            "dynamic_recipient_field": "owner_email",
        }
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data, follow_redirects=False)

        assert response.status_code == 303
        rule = session.exec(select(AlertRule)).one()
        assert rule.recipients == ""
        assert rule.dynamic_recipient_field == "owner_email"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_rejects_missing_recipients_when_no_dynamic_field(monkeypatch, session):
    data_source = _create_data_source(session)
    form_data = _valid_rule_form(data_source.id)
    form_data.update({"send_mode": "per_row", "recipients": "", "dynamic_recipient_field": ""})
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data)

        assert response.status_code == 400
        assert "请填写收件人或动态收件人字段" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_requires_suppression_key_when_enabled(monkeypatch, session):
    data_source = _create_data_source(session)
    form_data = _valid_rule_form(data_source.id)
    form_data.update({"suppress_duplicates": "on", "suppression_key_field": ""})
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data)

        assert response.status_code == 400
        assert "启用重复抑制时必须填写去重字段" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_rules_page_lists_existing_rules(monkeypatch, session):
    data_source = _create_data_source(session)
    session.add(
        AlertRule(
            name="库存预警",
            data_source_id=data_source.id,
            sql_text="select id from stock",
            cron_expression="0 8 * * *",
            recipients="ops@example.com",
            subject_template="库存预警",
            body_template="{{table}}",
            send_mode=SendMode.SUMMARY,
            enabled=True,
        )
    )
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules")

        assert response.status_code == 200
        assert "库存预警" in response.text
        assert "0 8 * * *" in response.text
        rule = session.exec(select(AlertRule)).one()
        assert f"/rules/{rule.id}/run" in response.text
        assert f"/rules/{rule.id}/edit" in response.text
        assert f"/rules/{rule.id}/copy" in response.text
        assert f"/rules/{rule.id}/versions" in response.text
        assert f'action="/rules/{rule.id}/delete"' in response.text
        assert 'name="_csrf_token"' in response.text
        assert "复制" in response.text
        assert "历史" in response.text
        assert "手动执行" in response.text
        assert "删除" in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_rules_page_uses_workbench_list_regions(monkeypatch, session):
    data_source = _create_data_source(session)
    _create_rule(session, data_source)
    client, get_settings, _ = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules")

        assert response.status_code == 200
        assert 'class="page-heading"' in response.text
        assert 'class="panel table-panel"' in response.text
        assert 'action="/rules/1/run"' in response.text
    finally:
        get_settings.cache_clear()


def test_rules_page_uses_semantic_status_classes(monkeypatch, session):
    data_source = _create_data_source(session)
    _create_rule(session, data_source, name="启用规则", enabled=True)
    _create_rule(session, data_source, name="停用规则", enabled=False)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules")

        assert response.status_code == 200
        assert '<span class="status-text status-success">启用</span>' in response.text
        assert '<span class="status-text status-muted">停用</span>' in response.text
        assert 'class="button button-danger"' in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_rule_form_keeps_sql_behaviors_inside_workbench_sections(monkeypatch, session):
    _create_data_source(session)
    client, get_settings, _ = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules/new")

        assert response.status_code == 200
        assert 'class="form-section sql-workspace"' in response.text
        assert "data-sql-check-button" in response.text
        assert "data-sql-preview-button" in response.text
    finally:
        get_settings.cache_clear()


def test_archive_rule_marks_it_inactive_and_preserves_related_records(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    rule.updated_at = datetime(2000, 1, 1)
    version = AlertRuleVersion(
        rule_id=rule.id,
        version_number=1,
        changed_by="admin",
        snapshot_json="{}",
    )
    execution_log = ExecutionLog(rule_id=rule.id, trigger_type=TriggerType.MANUAL)
    suppression = AlertSuppression(rule_id=rule.id, suppression_key="order-1")
    session.add(rule)
    session.add(version)
    session.add(execution_log)
    session.add(suppression)
    session.commit()
    session.refresh(version)
    session.refresh(execution_log)
    session.refresh(suppression)
    mail_log = MailLog(
        execution_log_id=execution_log.id,
        recipients="ops@example.com",
        subject="大额订单预警",
        status=MailStatus.SUCCESS,
    )
    session.add(mail_log)
    session.commit()
    session.refresh(mail_log)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        app.dependency_overrides.pop(require_csrf)
        csrf_token = _csrf_token(client)

        response = client.post(
            f"/rules/{rule.id}/delete",
            data={"_csrf_token": csrf_token},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/rules"
        session.refresh(rule)
        assert rule.enabled is False
        assert rule.archived_at is not None
        assert rule.updated_at > datetime(2000, 1, 1)
        assert session.get(AlertRuleVersion, version.id) is not None
        assert session.get(ExecutionLog, execution_log.id) is not None
        assert session.get(MailLog, mail_log.id) is not None
        assert session.get(AlertSuppression, suppression.id) is not None
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_archived_rules_are_omitted_from_rules_page_and_export(monkeypatch, session):
    data_source = _create_data_source(session)
    active_rule = _create_rule(session, data_source, name="活动规则")
    archived_rule = _create_rule(
        session,
        data_source,
        name="已归档规则",
        archived_at=datetime(2000, 1, 1),
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        rules_page = client.get("/rules")
        export = client.get("/rules/export.json")

        assert rules_page.status_code == 200
        assert active_rule.name in rules_page.text
        assert archived_rule.name not in rules_page.text
        assert [rule["name"] for rule in export.json()["rules"]] == [active_rule.name]
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("method", "path_suffix"),
    [
        ("get", "/edit"),
        ("get", "/copy"),
        ("get", "/versions"),
        ("post", ""),
        ("post", "/run"),
        ("post", "/delete"),
    ],
)
def test_archived_rule_actions_return_not_found(monkeypatch, session, method, path_suffix):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source, archived_at=datetime(2000, 1, 1))
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        path = f"/rules/{rule.id}{path_suffix}"
        if method == "post" and not path_suffix:
            response = client.post(path, data=_valid_rule_form(data_source.id))
        else:
            response = getattr(client, method)(path)

        assert response.status_code == 404
        assert response.json()["detail"] == "规则不存在"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_archive_rule_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, "/rules/1/delete")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_export_rules_json_includes_data_source_name(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(
        session,
        data_source,
        notes="迁移备注",
        send_mode=SendMode.PER_ROW,
        dynamic_recipient_field="owner_email",
        dynamic_cc_field="manager_email",
        suppress_duplicates=True,
        suppression_key_field="order_id",
        suppression_window_hours=12,
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules/export.json")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert 'filename="alert-rules.json"' in response.headers["content-disposition"]
        payload = response.json()
        assert payload["version"] == 1
        assert "exported_at" in payload
        assert payload["rules"][0]["name"] == rule.name
        assert payload["rules"][0]["data_source_name"] == data_source.name
        assert payload["rules"][0]["notes"] == "迁移备注"
        assert payload["rules"][0]["send_mode"] == "per_row"
        assert payload["rules"][0]["dynamic_recipient_field"] == "owner_email"
        assert payload["rules"][0]["dynamic_cc_field"] == "manager_email"
        assert payload["rules"][0]["suppress_duplicates"] is True
        assert payload["rules"][0]["suppression_key_field"] == "order_id"
        assert payload["rules"][0]["suppression_window_hours"] == 12
        assert "data_source_id" not in payload["rules"][0]
        assert "id" not in payload["rules"][0]
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_export_rules_json_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/rules/export.json")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_import_rules_json_creates_rules(monkeypatch, session):
    data_source = _create_data_source(session)
    payload = {
        "version": 1,
        "rules": [
            {
                "name": "导入规则",
                "data_source_name": data_source.name,
                "sql_text": "select id from imported_orders",
                "cron_expression": "15 8 * * 1-5",
                "recipients": "ops@example.com",
                "cc_recipients": "team@example.com",
                "subject_template": "导入预警",
                "body_template": "{{table}}",
                "send_mode": "per_row",
                "dynamic_recipient_field": "owner_email",
                "dynamic_cc_field": "manager_email",
                "query_timeout_seconds": 45,
                "max_rows": 100,
                "enabled": False,
                "notes": "导入备注",
                "suppress_duplicates": True,
                "suppression_key_field": "order_id",
                "suppression_window_hours": 12,
            }
        ],
    }
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/import",
            files={"file": ("rules.json", json.dumps(payload), "application/json")},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/rules?imported=1"
        rule = session.exec(select(AlertRule)).one()
        assert rule.name == "导入规则"
        assert rule.data_source_id == data_source.id
        assert rule.sql_text == "select id from imported_orders"
        assert rule.cron_expression == "15 8 * * 1-5"
        assert rule.recipients == "ops@example.com"
        assert rule.cc_recipients == "team@example.com"
        assert rule.subject_template == "导入预警"
        assert rule.body_template == "{{table}}"
        assert rule.send_mode == SendMode.PER_ROW
        assert rule.dynamic_recipient_field == "owner_email"
        assert rule.dynamic_cc_field == "manager_email"
        assert rule.query_timeout_seconds == 45
        assert rule.max_rows == 100
        assert rule.enabled is False
        assert rule.notes == "导入备注"
        assert rule.suppress_duplicates is True
        assert rule.suppression_key_field == "order_id"
        assert rule.suppression_window_hours == 12
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_import_rules_json_allows_dynamic_recipient_without_fixed_recipients(monkeypatch, session):
    data_source = _create_data_source(session)
    payload = {
        "version": 1,
        "rules": [
            {
                "name": "动态收件人导入",
                "data_source_name": data_source.name,
                "sql_text": "select id, owner_email from imported_orders",
                "cron_expression": "15 8 * * 1-5",
                "recipients": "",
                "subject_template": "导入预警",
                "body_template": "{{table}}",
                "send_mode": "per_row",
                "dynamic_recipient_field": "owner_email",
                "query_timeout_seconds": 45,
                "max_rows": 100,
                "enabled": True,
            }
        ],
    }
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/import",
            files={"file": ("rules.json", json.dumps(payload), "application/json")},
            follow_redirects=False,
        )

        assert response.status_code == 303
        rule = session.exec(select(AlertRule)).one()
        assert rule.recipients == ""
        assert rule.dynamic_recipient_field == "owner_email"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_import_rules_json_rejects_missing_recipients_and_dynamic_field(monkeypatch, session):
    data_source = _create_data_source(session)
    payload = {
        "version": 1,
        "rules": [
            {
                "name": "无收件人导入",
                "data_source_name": data_source.name,
                "sql_text": "select id from imported_orders",
                "cron_expression": "15 8 * * 1-5",
                "recipients": "",
                "subject_template": "导入预警",
                "body_template": "{{table}}",
                "send_mode": "per_row",
                "query_timeout_seconds": 45,
                "max_rows": 100,
                "enabled": True,
            }
        ],
    }
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/import",
            files={"file": ("rules.json", json.dumps(payload), "application/json")},
        )

        assert response.status_code == 400
        assert "第 1 条规则缺少收件人或动态收件人字段" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_import_rules_json_rejects_unknown_data_source_without_saving(monkeypatch, session):
    _create_data_source(session)
    payload = {
        "version": 1,
        "rules": [
            {
                "name": "导入规则",
                "data_source_name": "不存在的数据源",
                "sql_text": "select id from imported_orders",
                "cron_expression": "15 8 * * 1-5",
                "recipients": "ops@example.com",
                "subject_template": "导入预警",
                "body_template": "{{table}}",
                "send_mode": "summary",
                "query_timeout_seconds": 30,
                "max_rows": 500,
                "enabled": True,
            }
        ],
    }
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/import",
            files={"file": ("rules.json", json.dumps(payload), "application/json")},
        )

        assert response.status_code == 400
        assert "第 1 条规则的数据源不存在" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_import_rules_json_rejects_unsafe_sql_without_saving(monkeypatch, session):
    data_source = _create_data_source(session)
    payload = {
        "version": 1,
        "rules": [
            {
                "name": "导入规则",
                "data_source_name": data_source.name,
                "sql_text": "delete from imported_orders",
                "cron_expression": "15 8 * * 1-5",
                "recipients": "ops@example.com",
                "subject_template": "导入预警",
                "body_template": "{{table}}",
                "send_mode": "summary",
                "query_timeout_seconds": 30,
                "max_rows": 500,
                "enabled": True,
            }
        ],
    }
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/import",
            files={"file": ("rules.json", json.dumps(payload), "application/json")},
        )

        assert response.status_code == 400
        assert "第 1 条规则 SQL 无效：只允许 SELECT 查询" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_import_rules_json_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(
            client,
            "/rules/import",
            files={"file": ("rules.json", json.dumps({"version": 1, "rules": []}), "application/json")},
        )

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_new_rule_page_lists_data_sources(monkeypatch, session):
    _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules/new")

        assert response.status_code == 200
        assert "生产库" in response.text
        assert "data_source_id" in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_new_rule_page_renders_sql_check_button(monkeypatch, session):
    _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/rules/new")

        assert response.status_code == 200
        assert "检测 SQL" in response.text
        assert 'data-sql-check-button' in response.text
        assert 'data-endpoint="/rules/validate-sql"' in response.text
        assert "预览结果" in response.text
        assert 'data-sql-preview-button' in response.text
        assert 'data-endpoint="/rules/preview-sql"' in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


class FakeSyntaxSqlClient:
    def __init__(self, error=None, rows=None):
        self.error = error
        self.rows = rows or []
        self.checked_sql = None
        self.timeout_seconds = None
        self.queried_sql = None
        self.max_rows = None

    def query(self, sql, timeout_seconds, max_rows):
        self.queried_sql = sql
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows
        if self.error is not None:
            raise self.error
        return QueryResult(rows=self.rows)

    def validate_syntax(self, sql, timeout_seconds):
        self.checked_sql = sql
        self.timeout_seconds = timeout_seconds
        if self.error is not None:
            raise self.error


class FakeSmtpMailer:
    def __init__(self, result=None):
        self.result = result or MailSendResult(success=True)
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return self.result


def test_validate_rule_sql_accepts_sql_server_syntax(monkeypatch, session):
    data_source = _create_data_source(session)
    fake_client = FakeSyntaxSqlClient()
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(routes, "build_sql_client", lambda source: fake_client)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/validate-sql",
            data={"data_source_id": str(data_source.id), "sql_text": "select id from orders"},
        )

        assert response.status_code == 200
        assert response.json() == {"valid": True, "message": "SQL Server 语法检测通过"}
        assert fake_client.checked_sql == "select id from orders"
        assert fake_client.timeout_seconds == data_source.connect_timeout_seconds
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_validate_rule_sql_rejects_invalid_sql_before_connecting(monkeypatch, session):
    data_source = _create_data_source(session)
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(
        routes,
        "build_sql_client",
        lambda source: pytest.fail("should not connect when safety validation fails"),
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/validate-sql",
            data={"data_source_id": str(data_source.id), "sql_text": "delete from orders"},
        )

        assert response.status_code == 400
        assert response.json() == {"valid": False, "message": "只允许 SELECT 查询"}
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_validate_rule_sql_requires_data_source(monkeypatch, session):
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/validate-sql",
            data={"data_source_id": "", "sql_text": "select id from orders"},
        )

        assert response.status_code == 400
        assert response.json() == {"valid": False, "message": "请先选择数据源"}
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_validate_rule_sql_rejects_missing_data_source(monkeypatch, session):
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/validate-sql",
            data={"data_source_id": "999", "sql_text": "select id from orders"},
        )

        assert response.status_code == 400
        assert response.json() == {"valid": False, "message": "请选择有效的数据源"}
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_validate_rule_sql_returns_sql_server_syntax_error(monkeypatch, session):
    data_source = _create_data_source(session)
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(
        routes,
        "build_sql_client",
        lambda source: FakeSyntaxSqlClient(error=RuntimeError("Incorrect syntax near 'from'")),
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/validate-sql",
            data={"data_source_id": str(data_source.id), "sql_text": "select from orders"},
        )

        assert response.status_code == 400
        assert response.json() == {
            "valid": False,
            "message": "SQL Server 语法检测失败：Incorrect syntax near 'from'",
        }
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_validate_rule_sql_requires_admin_session(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, "/rules/validate-sql", data={"sql_text": "select 1"})

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_preview_rule_sql_returns_limited_rows(monkeypatch, session):
    data_source = _create_data_source(session)
    fake_client = FakeSyntaxSqlClient(rows=[{"id": 1, "amount": 120}, {"id": 2, "amount": 300}])
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(routes, "build_sql_client", lambda source: fake_client)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/preview-sql",
            data={
                "data_source_id": str(data_source.id),
                "sql_text": "select id, amount from orders",
                "query_timeout_seconds": "12",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "message": "查询成功，返回 2 行预览结果",
            "columns": ["id", "amount"],
            "rows": [{"id": 1, "amount": 120}, {"id": 2, "amount": 300}],
        }
        assert fake_client.queried_sql == "select id, amount from orders"
        assert fake_client.timeout_seconds == 12
        assert fake_client.max_rows == 5
        assert session.exec(select(ExecutionLog)).all() == []
        assert session.exec(select(MailLog)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_preview_rule_sql_reports_empty_rows(monkeypatch, session):
    data_source = _create_data_source(session)
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(routes, "build_sql_client", lambda source: FakeSyntaxSqlClient())
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/preview-sql",
            data={
                "data_source_id": str(data_source.id),
                "sql_text": "select id from orders where 1 = 0",
                "query_timeout_seconds": "12",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "message": "查询成功，暂无结果",
            "columns": [],
            "rows": [],
        }
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_preview_rule_sql_rejects_invalid_sql_before_connecting(monkeypatch, session):
    data_source = _create_data_source(session)
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(
        routes,
        "build_sql_client",
        lambda source: pytest.fail("should not connect when safety validation fails"),
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/preview-sql",
            data={
                "data_source_id": str(data_source.id),
                "sql_text": "update orders set amount = 1",
                "query_timeout_seconds": "12",
            },
        )

        assert response.status_code == 400
        assert response.json() == {"success": False, "message": "只允许 SELECT 查询"}
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_preview_rule_sql_requires_data_source(monkeypatch, session):
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/preview-sql",
            data={"data_source_id": "", "sql_text": "select 1", "query_timeout_seconds": "12"},
        )

        assert response.status_code == 400
        assert response.json() == {"success": False, "message": "请先选择数据源"}
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_preview_rule_sql_reports_query_error(monkeypatch, session):
    data_source = _create_data_source(session)
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(
        routes,
        "build_sql_client",
        lambda source: FakeSyntaxSqlClient(error=RuntimeError("timeout expired")),
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/preview-sql",
            data={
                "data_source_id": str(data_source.id),
                "sql_text": "select id from orders",
                "query_timeout_seconds": "12",
            },
        )

        assert response.status_code == 400
        assert response.json() == {"success": False, "message": "预览失败：timeout expired"}
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_preview_rule_sql_requires_admin_session(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, "/rules/preview-sql", data={"sql_text": "select 1"})

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_edit_rule_page_prefills_existing_rule(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(
        session,
        data_source,
        name="库存预警",
        sql_text="select id from stock",
        send_mode=SendMode.PER_ROW,
        dynamic_recipient_field="owner_email",
        dynamic_cc_field="manager_email",
        suppress_duplicates=True,
        suppression_key_field="stock_id",
        suppression_window_hours=8,
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get(f"/rules/{rule.id}/edit")

        assert response.status_code == 200
        assert "编辑规则" in response.text
        assert "库存预警" in response.text
        assert "select id from stock" in response.text
        assert f'action="/rules/{rule.id}"' in response.text
        assert 'name="dynamic_recipient_field" value="owner_email"' in response.text
        assert 'name="dynamic_cc_field" value="manager_email"' in response.text
        assert 'name="suppress_duplicates" type="checkbox" checked' in response.text
        assert 'name="suppression_key_field" value="stock_id"' in response.text
        assert 'name="suppression_window_hours" type="number" value="8"' in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_copy_rule_page_prefills_new_rule_form(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(
        session,
        data_source,
        name="库存预警",
        sql_text="select id from stock",
        cron_expression="15 9 * * 1-5",
        recipients="ops@example.com",
        cc_recipients="team@example.com",
        subject_template="库存 {{row_count}}",
        body_template="{{table}}",
        send_mode=SendMode.PER_ROW,
        dynamic_recipient_field="owner_email",
        dynamic_cc_field="manager_email",
        query_timeout_seconds=45,
        max_rows=100,
        enabled=False,
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get(f"/rules/{rule.id}/copy")

        assert response.status_code == 200
        assert "复制规则" in response.text
        assert "库存预警 副本" in response.text
        assert "select id from stock" in response.text
        assert "15 9 * * 1-5" in response.text
        assert "ops@example.com" in response.text
        assert "team@example.com" in response.text
        assert 'name="dynamic_recipient_field" value="owner_email"' in response.text
        assert 'name="dynamic_cc_field" value="manager_email"' in response.text
        assert "库存 {{row_count}}" in response.text
        assert "{{table}}" in response.text
        assert 'option value="per_row" selected' in response.text
        assert 'value="45"' in response.text
        assert 'value="100"' in response.text
        assert 'action="/rules"' in response.text
        assert 'name="enabled" type="checkbox" checked' in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_update_rule_persists_changes(monkeypatch, session):
    data_source = _create_data_source(session)
    other_source = SqlDataSource(
        name="备库",
        host="backup.example.com",
        port=1433,
        database="erp_backup",
        username="readonly",
        encrypted_password="encrypted",
        enabled=True,
    )
    session.add(other_source)
    session.commit()
    session.refresh(other_source)
    rule = _create_rule(session, data_source)
    form_data = _valid_rule_form(other_source.id)
    form_data.update(
        {
            "name": "更新后的规则",
            "sql_text": "select id from updated_orders",
            "cron_expression": "30 8 * * 1-5",
            "recipients": "owner@example.com",
            "cc_recipients": "team@example.com",
            "subject_template": "更新主题",
            "body_template": "更新正文 {{table}}",
            "send_mode": "per_row",
            "dynamic_recipient_field": "owner_email",
            "dynamic_cc_field": "manager_email",
            "query_timeout_seconds": "45",
            "max_rows": "100",
            "suppress_duplicates": "on",
            "suppression_key_field": "customer_id",
            "suppression_window_hours": "6",
        }
    )
    form_data.pop("enabled")
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/rules/{rule.id}", data=form_data, follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/rules"
        session.refresh(rule)
        assert rule.name == "更新后的规则"
        assert rule.data_source_id == other_source.id
        assert rule.sql_text == "select id from updated_orders"
        assert rule.cron_expression == "30 8 * * 1-5"
        assert rule.recipients == "owner@example.com"
        assert rule.cc_recipients == "team@example.com"
        assert rule.subject_template == "更新主题"
        assert rule.body_template == "更新正文 {{table}}"
        assert rule.send_mode == SendMode.PER_ROW
        assert rule.dynamic_recipient_field == "owner_email"
        assert rule.dynamic_cc_field == "manager_email"
        assert rule.query_timeout_seconds == 45
        assert rule.max_rows == 100
        assert rule.enabled is False
        assert rule.suppress_duplicates is True
        assert rule.suppression_key_field == "customer_id"
        assert rule.suppression_window_hours == 6
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_update_rule_creates_version_snapshot_before_changes(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(
        session,
        data_source,
        name="旧规则",
        sql_text="select id from old_orders",
        cron_expression="0 9 * * *",
        recipients="old@example.com",
        cc_recipients="old-team@example.com",
        subject_template="旧主题",
        body_template="旧正文 {{table}}",
        send_mode=SendMode.SUMMARY,
        query_timeout_seconds=30,
        max_rows=500,
        enabled=True,
        dynamic_recipient_field="",
        dynamic_cc_field="",
        suppress_duplicates=True,
        suppression_key_field="order_id",
        suppression_window_hours=24,
    )
    form_data = _valid_rule_form(data_source.id)
    form_data.update(
        {
            "name": "新规则",
            "sql_text": "select id from new_orders",
            "recipients": "new@example.com",
            "subject_template": "新主题",
            "body_template": "新正文 {{table}}",
        }
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/rules/{rule.id}", data=form_data, follow_redirects=False)

        assert response.status_code == 303
        version = session.exec(select(AlertRuleVersion)).one()
        snapshot = json.loads(version.snapshot_json)
        assert version.rule_id == rule.id
        assert version.version_number == 1
        assert version.changed_by == "admin"
        assert snapshot["id"] == rule.id
        assert snapshot["name"] == "旧规则"
        assert snapshot["sql_text"] == "select id from old_orders"
        assert snapshot["cron_expression"] == "0 9 * * *"
        assert snapshot["recipients"] == "old@example.com"
        assert snapshot["cc_recipients"] == "old-team@example.com"
        assert snapshot["subject_template"] == "旧主题"
        assert snapshot["body_template"] == "旧正文 {{table}}"
        assert snapshot["send_mode"] == "summary"
        assert snapshot["query_timeout_seconds"] == 30
        assert snapshot["max_rows"] == 500
        assert snapshot["enabled"] is True
        assert snapshot["suppress_duplicates"] is True
        assert snapshot["suppression_key_field"] == "order_id"
        assert snapshot["suppression_window_hours"] == 24
        session.refresh(rule)
        assert rule.name == "新规则"
        assert rule.sql_text == "select id from new_orders"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_update_rule_version_numbers_increment(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        first_form = _valid_rule_form(data_source.id)
        first_form["name"] = "第一次更新"
        second_form = _valid_rule_form(data_source.id)
        second_form["name"] = "第二次更新"

        assert client.post(f"/rules/{rule.id}", data=first_form, follow_redirects=False).status_code == 303
        assert client.post(f"/rules/{rule.id}", data=second_form, follow_redirects=False).status_code == 303

        versions = session.exec(
            select(AlertRuleVersion).where(AlertRuleVersion.rule_id == rule.id).order_by(AlertRuleVersion.version_number)
        ).all()
        assert [version.version_number for version in versions] == [1, 2]
        assert json.loads(versions[0].snapshot_json)["name"] == "大额订单"
        assert json.loads(versions[1].snapshot_json)["name"] == "第一次更新"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_update_rule_invalid_form_does_not_create_version(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    form_data = _valid_rule_form(data_source.id)
    form_data["sql_text"] = "delete from orders"
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/rules/{rule.id}", data=form_data)

        assert response.status_code == 400
        assert session.exec(select(AlertRuleVersion)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_rule_versions_page_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/rules/1/versions")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_rule_versions_page_renders_snapshots(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source, name="当前规则")
    version = AlertRuleVersion(
        rule_id=rule.id,
        version_number=1,
        changed_by="admin",
        snapshot_json=json.dumps(
            {
                "name": "历史规则",
                "data_source_id": data_source.id,
                "sql_text": "select id from old_orders",
                "cron_expression": "0 7 * * *",
                "recipients": "old@example.com",
                "cc_recipients": "team@example.com",
                "subject_template": "旧主题",
                "body_template": "旧正文",
                "send_mode": "summary",
                "query_timeout_seconds": 30,
                "max_rows": 500,
                "enabled": True,
                "dynamic_recipient_field": "",
                "dynamic_cc_field": "",
                "suppress_duplicates": False,
                "suppression_key_field": "",
                "suppression_window_hours": 24,
            },
            ensure_ascii=False,
        ),
    )
    session.add(version)
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get(f"/rules/{rule.id}/versions")

        assert response.status_code == 200
        assert "版本历史" in response.text
        assert "当前规则" in response.text
        assert "#1" in response.text
        assert "admin" in response.text
        assert "历史规则" in response.text
        assert "select id from old_orders" in response.text
        assert "old@example.com" in response.text
        assert f"/rules/{rule.id}/edit" in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_rejects_non_select_sql_without_saving(monkeypatch, session):
    data_source = _create_data_source(session)
    form_data = _valid_rule_form(data_source.id)
    form_data["sql_text"] = "delete from orders"
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data)

        assert response.status_code == 400
        assert "只允许 SELECT 查询" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_validates_sql_before_data_source(monkeypatch, session):
    form_data = _valid_rule_form(999)
    form_data["sql_text"] = "delete from orders"
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data)

        assert response.status_code == 400
        assert "只允许 SELECT 查询" in response.text
        assert "请选择有效的数据源" not in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_rejects_invalid_data_source_without_saving(monkeypatch, session):
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=_valid_rule_form(999))

        assert response.status_code == 400
        assert "请选择有效的数据源" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_rejects_invalid_cron_without_saving(monkeypatch, session):
    data_source = _create_data_source(session)
    form_data = _valid_rule_form(data_source.id)
    form_data["cron_expression"] = "not a cron"
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data)

        assert response.status_code == 400
        assert "Cron 表达式无效" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_rule_rejects_invalid_send_mode_without_saving(monkeypatch, session):
    data_source = _create_data_source(session)
    form_data = _valid_rule_form(data_source.id)
    form_data["send_mode"] = "unknown"
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=form_data)

        assert response.status_code == 400
        assert "unknown" in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_settings_page_lists_data_sources_and_smtp_configs(monkeypatch, session):
    _create_data_source(session)
    session.add(
        SmtpConfig(
            host="smtp.example.com",
            port=587,
            username="mailer",
            encrypted_password="encrypted",
            sender="alerts@example.com",
            enabled=True,
        )
    )
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/settings")

        assert response.status_code == 200
        assert "生产库" in response.text
        data_source = session.exec(select(SqlDataSource)).one()
        assert f"/settings/sql-server/{data_source.id}/edit" in response.text
        assert f"/settings/sql-server/{data_source.id}/test" in response.text
        assert "测试连接" in response.text
        assert "table-actions" in response.text
        assert "smtp.example.com" in response.text
        smtp_config = session.exec(select(SmtpConfig)).one()
        assert f"/settings/smtp/{smtp_config.id}/test" in response.text
        assert "测试发送" in response.text
        assert "encrypted" not in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_settings_page_uses_semantic_status_classes(monkeypatch, session):
    _create_data_source(session)
    session.add(
        SqlDataSource(
            name="停用库",
            host="disabled-db.example.com",
            port=1433,
            database="archive",
            username="readonly",
            encrypted_password="encrypted",
            enabled=False,
        )
    )
    _create_smtp_config(session, enabled=True)
    _create_smtp_config(session, enabled=False)
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/settings")

        assert response.status_code == 200
        assert response.text.count('<span class="status-text status-success">启用</span>') >= 2
        assert response.text.count('<span class="status-text status-muted">停用</span>') >= 2
        assert response.text.count('class="button button-danger"') >= 2
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_settings_page_includes_sql_server_delete_action(monkeypatch, session):
    data_source = _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/settings")

        assert response.status_code == 200
        assert f"/settings/sql-server/{data_source.id}/delete" in response.text
        assert "确认删除该数据源吗？" in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_delete_sql_server_settings_deletes_unreferenced_source(monkeypatch, session):
    data_source = _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            f"/settings/sql-server/{data_source.id}/delete", follow_redirects=False
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/settings"
        assert session.get(SqlDataSource, data_source.id) is None
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_delete_sql_server_settings_rejects_referenced_source(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source, name="订单预警")
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/settings/sql-server/{data_source.id}/delete")

        assert response.status_code == 400
        assert "数据源正在被规则引用：订单预警" in response.text
        assert session.get(SqlDataSource, data_source.id) is not None
        assert session.get(AlertRule, rule.id) is not None
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_delete_sql_server_settings_requires_admin_session(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, "/settings/sql-server/1/delete")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_settings_page_includes_smtp_edit_and_delete_actions(monkeypatch, session):
    smtp_config = _create_smtp_config(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/settings")

        assert response.status_code == 200
        assert f"/settings/smtp/{smtp_config.id}/edit" in response.text
        assert f"/settings/smtp/{smtp_config.id}/delete" in response.text
        assert "确认删除该 SMTP 配置吗？" in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_edit_smtp_settings_page_prefills_existing_config(monkeypatch, session):
    smtp_config = _create_smtp_config(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get(f"/settings/smtp/{smtp_config.id}/edit")

        assert response.status_code == 200
        assert "编辑 SMTP 配置" in response.text
        assert smtp_config.host in response.text
        assert smtp_config.username in response.text
        assert smtp_config.encrypted_password not in response.text
        assert f'action="/settings/smtp/{smtp_config.id}"' in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_update_smtp_settings_preserves_blank_password(monkeypatch, session):
    smtp_config = _create_smtp_config(session)
    original_encrypted_password = smtp_config.encrypted_password
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            f"/settings/smtp/{smtp_config.id}",
            data={
                "host": "new-smtp.example.com",
                "port": "465",
                "username": "new-mailer",
                "password": "",
                "sender": "new-alerts@example.com",
                "use_ssl": "on",
                "timeout_seconds": "20",
                "enabled": "",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/settings"
        session.refresh(smtp_config)
        assert smtp_config.host == "new-smtp.example.com"
        assert smtp_config.port == 465
        assert smtp_config.username == "new-mailer"
        assert smtp_config.encrypted_password == original_encrypted_password
        assert smtp_config.sender == "new-alerts@example.com"
        assert smtp_config.use_tls is False
        assert smtp_config.use_ssl is True
        assert smtp_config.timeout_seconds == 20
        assert smtp_config.enabled is False
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_update_smtp_settings_replaces_password_when_provided(monkeypatch, session):
    smtp_config = _create_smtp_config(session)
    original_encrypted_password = smtp_config.encrypted_password
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            f"/settings/smtp/{smtp_config.id}",
            data={
                "host": smtp_config.host,
                "port": "587",
                "username": smtp_config.username,
                "password": "replacement-password",
                "sender": smtp_config.sender,
                "use_tls": "on",
                "timeout_seconds": "10",
                "enabled": "on",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        session.refresh(smtp_config)
        assert smtp_config.encrypted_password != original_encrypted_password
        assert smtp_config.encrypted_password != "replacement-password"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_delete_smtp_settings_deletes_config(monkeypatch, session):
    smtp_config = _create_smtp_config(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/settings/smtp/{smtp_config.id}/delete", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/settings"
        assert session.get(SmtpConfig, smtp_config.id) is None
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize("path", ["/settings/smtp/1", "/settings/smtp/1/delete"])
def test_smtp_settings_update_and_delete_require_admin_session(monkeypatch, path):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, path)

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_test_sql_server_settings_reports_success(monkeypatch, session):
    data_source = _create_data_source(session)
    fake_client = FakeSyntaxSqlClient()
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(routes, "build_sql_client", lambda source: fake_client)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/settings/sql-server/{data_source.id}/test")

        assert response.status_code == 200
        assert "数据源 生产库 连接成功" in response.text
        assert fake_client.queried_sql == "select 1 as ok"
        assert fake_client.timeout_seconds == data_source.connect_timeout_seconds
        assert fake_client.max_rows == 1
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_test_sql_server_settings_reports_failure(monkeypatch, session):
    data_source = _create_data_source(session)
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(
        routes,
        "build_sql_client",
        lambda source: FakeSyntaxSqlClient(error=RuntimeError("login failed")),
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/settings/sql-server/{data_source.id}/test")

        assert response.status_code == 400
        assert "连接失败：login failed" in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_test_sql_server_settings_requires_admin_session(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, "/settings/sql-server/1/test")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_test_smtp_settings_reports_success(monkeypatch, session):
    smtp_config = _create_smtp_config(session)
    fake_mailer = FakeSmtpMailer()
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(routes, "build_smtp_mailer", lambda config: fake_mailer)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/settings/smtp/{smtp_config.id}/test")

        assert response.status_code == 200
        assert "SMTP 测试邮件发送成功" in response.text
        assert len(fake_mailer.messages) == 1
        message = fake_mailer.messages[0]
        assert message.recipients == [smtp_config.sender]
        assert message.cc_recipients == []
        assert "SQL 预警系统 SMTP 测试" in message.subject
        assert "SMTP 配置已可用" in message.html_body
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_test_smtp_settings_reports_failure(monkeypatch, session):
    smtp_config = _create_smtp_config(session)
    fake_mailer = FakeSmtpMailer(MailSendResult(success=False, error_message="auth failed"))
    routes = importlib.import_module("app.routes")
    monkeypatch.setattr(routes, "build_smtp_mailer", lambda config: fake_mailer)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/settings/smtp/{smtp_config.id}/test")

        assert response.status_code == 400
        assert "SMTP 测试发送失败：auth failed" in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_test_smtp_settings_requires_admin_session(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, "/settings/smtp/1/test")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_edit_sql_server_settings_page_prefills_existing_data_source(monkeypatch, session):
    data_source = _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get(f"/settings/sql-server/{data_source.id}/edit")

        assert response.status_code == 200
        assert "编辑数据源" in response.text
        assert "生产库" in response.text
        assert "db.example.com" in response.text
        assert "readonly" in response.text
        assert "encrypted" not in response.text
        assert f'action="/settings/sql-server/{data_source.id}"' in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_update_sql_server_settings_preserves_password_when_blank(monkeypatch, session):
    data_source = _create_data_source(session)
    original_encrypted_password = data_source.encrypted_password
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            f"/settings/sql-server/{data_source.id}",
            data={
                "name": "生产库更新",
                "host": "new-db.example.com",
                "port": "14330",
                "database": "erp2",
                "username": "readonly2",
                "password": "",
                "enabled": "",
                "connect_timeout_seconds": "25",
                "odbc_driver": "ODBC Driver 17 for SQL Server",
                "server_override": "new-db.example.com,14330",
                "encrypt": "no",
                "trust_server_certificate": "yes",
                "extra_params": "MultiSubnetFailover=Yes;",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/settings"
        session.refresh(data_source)
        assert data_source.name == "生产库更新"
        assert data_source.host == "new-db.example.com"
        assert data_source.port == 14330
        assert data_source.database == "erp2"
        assert data_source.username == "readonly2"
        assert data_source.encrypted_password == original_encrypted_password
        assert data_source.enabled is False
        assert data_source.connect_timeout_seconds == 25
        assert data_source.odbc_driver == "ODBC Driver 17 for SQL Server"
        assert data_source.server_override == "new-db.example.com,14330"
        assert data_source.encrypt == "no"
        assert data_source.trust_server_certificate == "yes"
        assert data_source.extra_params == "MultiSubnetFailover=Yes;"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_update_sql_server_settings_replaces_password_when_provided(monkeypatch, session):
    data_source = _create_data_source(session)
    original_encrypted_password = data_source.encrypted_password
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            f"/settings/sql-server/{data_source.id}",
            data={
                "name": "生产库",
                "host": "db.example.com",
                "port": "1433",
                "database": "erp",
                "username": "readonly",
                "password": "new-password",
                "enabled": "on",
                "connect_timeout_seconds": "10",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        session.refresh(data_source)
        assert data_source.encrypted_password != original_encrypted_password
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_sql_server_settings_requires_admin_session(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, "/settings/sql-server", data={"name": "生产库"})

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_create_sql_server_settings_encrypts_password(monkeypatch, session):
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/settings/sql-server",
            data={
                "name": "生产库",
                "host": "db.example.com",
                "port": "1433",
                "database": "erp",
                "username": "readonly",
                "password": "plain-password",
                "enabled": "on",
                "connect_timeout_seconds": "15",
                "odbc_driver": "ODBC Driver 17 for SQL Server",
                "server_override": r"db.example.com\\REPORTING",
                "encrypt": "optional",
                "trust_server_certificate": "no",
                "extra_params": "ApplicationIntent=ReadOnly;",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        data_source = session.exec(select(SqlDataSource)).one()
        assert data_source.encrypted_password != "plain-password"
        assert data_source.connect_timeout_seconds == 15
        assert data_source.odbc_driver == "ODBC Driver 17 for SQL Server"
        assert data_source.server_override == r"db.example.com\\REPORTING"
        assert data_source.encrypt == "optional"
        assert data_source.trust_server_certificate == "no"
        assert data_source.extra_params == "ApplicationIntent=ReadOnly;"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_sql_server_settings_rejects_duplicate_name(monkeypatch, session):
    _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/settings/sql-server",
            data={
                "name": "生产库",
                "host": "other.example.com",
                "port": "1433",
                "database": "erp2",
                "username": "readonly",
                "password": "plain-password",
                "enabled": "on",
                "connect_timeout_seconds": "15",
            },
        )

        assert response.status_code == 400
        assert "数据源名称已存在" in response.text
        assert len(session.exec(select(SqlDataSource)).all()) == 1
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_smtp_settings_encrypts_password(monkeypatch, session):
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/settings/smtp",
            data={
                "host": "smtp.example.com",
                "port": "587",
                "username": "mailer",
                "password": "smtp-password",
                "sender": "alerts@example.com",
                "use_tls": "on",
                "timeout_seconds": "20",
                "enabled": "on",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        smtp_config = session.exec(select(SmtpConfig)).one()
        assert smtp_config.encrypted_password != "smtp-password"
        assert smtp_config.use_tls is True
        assert smtp_config.use_ssl is False
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_creating_enabled_smtp_config_disables_existing_enabled_config(monkeypatch, session):
    existing = _create_smtp_config(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/settings/smtp",
            data={
                "host": "replacement.example.com",
                "port": "587",
                "username": "mailer",
                "password": "smtp-password",
                "sender": "alerts@example.com",
                "enabled": "on",
                "timeout_seconds": "10",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        configs = session.exec(select(SmtpConfig).order_by(SmtpConfig.id)).all()
        assert [(config.id, config.enabled) for config in configs] == [
            (existing.id, False),
            (configs[1].id, True),
        ]
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_enabling_smtp_config_disables_other_enabled_config(monkeypatch, session):
    existing = _create_smtp_config(session)
    replacement = _create_smtp_config(session, enabled=False)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            f"/settings/smtp/{replacement.id}",
            data={
                "host": replacement.host,
                "port": "587",
                "username": replacement.username,
                "password": "",
                "sender": replacement.sender,
                "enabled": "on",
                "timeout_seconds": "10",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert session.get(SmtpConfig, existing.id).enabled is False
        assert session.get(SmtpConfig, replacement.id).enabled is True
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_creating_enabled_smtp_config_rolls_back_when_commit_fails(monkeypatch, session):
    existing = _create_smtp_config(session)
    commit = Mock(side_effect=RuntimeError("injected commit failure"))
    rollback = Mock(wraps=session.rollback)
    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(session, "rollback", rollback)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/settings/smtp",
            data={
                "host": "replacement.example.com",
                "port": "587",
                "username": "mailer",
                "password": "smtp-password",
                "sender": "alerts@example.com",
                "enabled": "on",
                "timeout_seconds": "10",
            },
            follow_redirects=False,
        )

        assert response.status_code == 500
        commit.assert_called_once_with()
        rollback.assert_called_once_with()
        with Session(session.get_bind()) as verification_session:
            configs = verification_session.exec(select(SmtpConfig).order_by(SmtpConfig.id)).all()
        assert [(config.id, config.enabled) for config in configs] == [(existing.id, True)]
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_enabling_smtp_config_rolls_back_when_commit_fails(monkeypatch, session):
    existing = _create_smtp_config(session)
    replacement = _create_smtp_config(session, enabled=False)
    commit = Mock(side_effect=RuntimeError("injected commit failure"))
    rollback = Mock(wraps=session.rollback)
    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(session, "rollback", rollback)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            f"/settings/smtp/{replacement.id}",
            data={
                "host": "replacement.example.com",
                "port": "465",
                "username": "replacement-mailer",
                "password": "replacement-password",
                "sender": "replacement@example.com",
                "enabled": "on",
                "timeout_seconds": "20",
            },
            follow_redirects=False,
        )

        assert response.status_code == 500
        commit.assert_called_once_with()
        rollback.assert_called_once_with()
        with Session(session.get_bind()) as verification_session:
            configs = verification_session.exec(select(SmtpConfig).order_by(SmtpConfig.id)).all()
        assert [(config.id, config.enabled) for config in configs] == [
            (existing.id, True),
            (replacement.id, False),
        ]
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("use_tls", "use_ssl", "expected_status"),
    [("on", "on", 400), ("on", None, 303), (None, "on", 303), (None, None, 303)],
)
def test_smtp_form_rejects_combined_tls_and_ssl(monkeypatch, session, use_tls, use_ssl, expected_status):
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        form = {
            "host": "smtp.example.com",
            "port": "587",
            "username": "mailer",
            "password": "never-render-this-password",
            "sender": "alerts@example.com",
            "timeout_seconds": "10",
        }
        if use_tls is not None:
            form["use_tls"] = use_tls
        if use_ssl is not None:
            form["use_ssl"] = use_ssl

        response = client.post("/settings/smtp", data=form, follow_redirects=False)

        assert response.status_code == expected_status
        if expected_status == 400:
            assert "TLS 和 SSL 不能同时启用" in response.text
            assert "never-render-this-password" not in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_updating_smtp_config_rejects_combined_tls_and_ssl(monkeypatch, session):
    smtp_config = _create_smtp_config(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            f"/settings/smtp/{smtp_config.id}",
            data={
                "host": smtp_config.host,
                "port": "587",
                "username": smtp_config.username,
                "password": "never-render-this-password",
                "sender": smtp_config.sender,
                "use_tls": "on",
                "use_ssl": "on",
                "timeout_seconds": "10",
            },
        )

        assert response.status_code == 400
        assert "TLS 和 SSL 不能同时启用" in response.text
        assert "never-render-this-password" not in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("use_tls", "use_ssl"),
    [(True, False), (False, True), (False, False)],
)
def test_updating_smtp_config_saves_legal_tls_ssl_combinations(
    monkeypatch, session, use_tls, use_ssl
):
    smtp_config = _create_smtp_config(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        form = {
            "host": smtp_config.host,
            "port": "587",
            "username": smtp_config.username,
            "password": "",
            "sender": smtp_config.sender,
            "timeout_seconds": "10",
        }
        if use_tls:
            form["use_tls"] = "on"
        if use_ssl:
            form["use_ssl"] = "on"

        response = client.post(
            f"/settings/smtp/{smtp_config.id}",
            data=form,
            follow_redirects=False,
        )

        assert response.status_code == 303
        session.expire(smtp_config)
        assert (smtp_config.use_tls, smtp_config.use_ssl) == (use_tls, use_ssl)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_logs_page_lists_execution_and_mail_logs(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = AlertRule(
        name="日志规则",
        data_source_id=data_source.id,
        sql_text="select id from orders",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="大额订单预警",
        body_template="{{table}}",
        send_mode=SendMode.SUMMARY,
        enabled=True,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    execution_log = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        row_count=2,
        email_count=1,
    )
    session.add(execution_log)
    session.commit()
    session.refresh(execution_log)
    session.add(
        MailLog(
            execution_log_id=execution_log.id,
            recipients="ops@example.com",
            subject="大额订单预警",
            status=MailStatus.SUCCESS,
        )
    )
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/logs")

        assert response.status_code == 200
        assert response.text.count('class="panel table-panel"') >= 2
        assert "/logs/executions.csv" in response.text
        assert "/logs/mails.csv" in response.text
        assert "导出执行日志" in response.text
        assert "导出邮件日志" in response.text
        assert 'name="execution_status"' in response.text
        assert 'name="trigger_type"' in response.text
        assert 'name="rule_id"' in response.text
        assert 'name="mail_status"' in response.text
        assert 'name="keyword"' in response.text
        assert "大额订单预警" in response.text
        assert "ops@example.com" in response.text
        assert "manual" in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_logs_page_keeps_filters_and_other_log_page_in_pagination_links(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    session.add_all(
        [
            ExecutionLog(
                rule_id=rule.id,
                trigger_type=TriggerType.MANUAL,
                status=ExecutionStatus.FAILED,
                error_message=f"execution-problem-{index}",
            )
            for index in range(11)
        ]
    )
    session.commit()
    execution_log = session.exec(select(ExecutionLog)).first()
    assert execution_log is not None
    session.add_all(
        [
            MailLog(
                execution_log_id=execution_log.id,
                recipients="ops@example.com",
                subject=f"mail-problem-{index}",
                status=MailStatus.FAILED,
            )
            for index in range(31)
        ]
    )
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get(
            "/logs?execution_status=failed&trigger_type=manual"
            f"&rule_id={rule.id}&mail_status=failed&keyword=problem"
            "&execution_page=1&mail_page=3&page_size=10"
        )

        assert response.status_code == 200
        assert "总计 11 条，第 1/2 页" in response.text
        assert "总计 31 条，第 3/4 页" in response.text
        assert 'name="page_size" min="10" max="200" value="10"' in response.text
        assert (
            f"execution_status=failed&amp;trigger_type=manual&amp;rule_id={rule.id}"
            "&amp;mail_status=failed&amp;keyword=problem&amp;execution_page=2"
            "&amp;mail_page=3&amp;page_size=10"
        ) in response.text
        assert (
            f"execution_status=failed&amp;trigger_type=manual&amp;rule_id={rule.id}"
            "&amp;mail_status=failed&amp;keyword=problem&amp;execution_page=1"
            "&amp;mail_page=2&amp;page_size=10"
        ) in response.text
        assert "mail-problem-" in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_table_panel_section_headings_align_with_table_content():
    stylesheet = Path("app/static/styles.css").read_text(encoding="utf-8")

    assert ".table-panel > .section-heading {\n  padding: 18px 18px 0;\n}" in stylesheet


def test_danger_button_and_status_variants_have_semantic_styles():
    stylesheet = Path("app/static/styles.css").read_text(encoding="utf-8")

    assert "--danger-hover: #912018;" in stylesheet
    assert ".button-danger {\n  background: var(--danger);\n}" in stylesheet
    assert ".button-danger:hover {\n  background: var(--danger-hover);\n}" in stylesheet
    assert ".status-success {\n  color: var(--success);\n}" in stylesheet
    assert ".status-warning {\n  color: var(--warning);\n}" in stylesheet
    assert ".status-danger {\n  color: var(--danger);\n}" in stylesheet
    assert ".status-muted {\n  color: var(--muted);\n}" in stylesheet


def test_two_column_children_can_shrink_below_intrinsic_content_width():
    stylesheet = Path("app/static/styles.css").read_text(encoding="utf-8")

    assert ".two-column > * {\n  min-width: 0;\n}" in stylesheet


def test_logs_page_uses_semantic_status_classes(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    success_log = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        status=ExecutionStatus.SUCCESS,
    )
    running_log = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        status=ExecutionStatus.RUNNING,
    )
    partial_failure_log = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        status=ExecutionStatus.PARTIAL_FAILED,
    )
    session.add(success_log)
    session.add(running_log)
    session.add(partial_failure_log)
    session.commit()
    session.refresh(success_log)
    session.add(
        MailLog(
            execution_log_id=success_log.id,
            recipients="ops@example.com",
            subject="发送失败",
            status=MailStatus.FAILED,
        )
    )
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/logs")

        assert response.status_code == 200
        assert '<span class="status-text status-success">success</span>' in response.text
        assert '<span class="status-text status-warning">running</span>' in response.text
        assert '<span class="status-text status-danger">partial_failed</span>' in response.text
        assert '<span class="status-text status-danger">failed</span>' in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_logs_page_filters_execution_logs_by_status_trigger_and_rule(monkeypatch, session):
    data_source = _create_data_source(session)
    matching_rule = _create_rule(session, data_source, name="匹配规则")
    other_rule = _create_rule(
        session,
        data_source,
        name="其他规则",
        sql_text="select id from invoices",
        subject_template="其他预警",
    )
    session.add(
        ExecutionLog(
            rule_id=matching_rule.id,
            trigger_type=TriggerType.MANUAL,
            status=ExecutionStatus.FAILED,
            error_message="目标执行错误",
        )
    )
    session.add(
        ExecutionLog(
            rule_id=other_rule.id,
            trigger_type=TriggerType.SCHEDULED,
            status=ExecutionStatus.SUCCESS,
            error_message="另一条执行记录",
        )
    )
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get(
            f"/logs?execution_status=failed&trigger_type=manual&rule_id={matching_rule.id}"
        )

        assert response.status_code == 200
        assert "目标执行错误" in response.text
        assert "另一条执行记录" not in response.text
        assert 'value="failed" selected' in response.text
        assert 'value="manual" selected' in response.text
        assert f'value="{matching_rule.id}"' in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_logs_page_filters_mail_logs_by_status_and_keyword(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    execution_log = ExecutionLog(rule_id=rule.id, trigger_type=TriggerType.MANUAL)
    session.add(execution_log)
    session.commit()
    session.refresh(execution_log)
    session.add(
        MailLog(
            execution_log_id=execution_log.id,
            recipients="ops@example.com",
            subject="大额订单预警",
            status=MailStatus.FAILED,
            error_message="smtp refused",
        )
    )
    session.add(
        MailLog(
            execution_log_id=execution_log.id,
            recipients="boss@example.com",
            subject="库存预警",
            status=MailStatus.SUCCESS,
        )
    )
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/logs?mail_status=failed&keyword=ops")

        assert response.status_code == 200
        assert "ops@example.com" in response.text
        assert "大额订单预警" in response.text
        assert "boss@example.com" not in response.text
        assert "库存预警" not in response.text
        assert 'value="failed" selected' in response.text
        assert 'value="ops"' in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_export_execution_logs_csv(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    execution_log = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        status=ExecutionStatus.FAILED,
        row_count=3,
        email_count=1,
        duration_ms=250,
        error_type="RuntimeError",
        error_message="=1+1",
    )
    session.add(execution_log)
    session.commit()
    session.refresh(execution_log)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/logs/executions.csv")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert 'filename="execution-logs.csv"' in response.headers["content-disposition"]
        assert response.content.startswith("\ufeff".encode())
        csv_text = response.content.decode("utf-8-sig")
        assert "ID,规则ID,触发方式,状态,开始时间,结束时间,返回行数,邮件数,耗时毫秒,错误类型,错误信息" in csv_text
        assert f"{execution_log.id},{rule.id},manual,failed," in csv_text
        assert ",3,1,250,RuntimeError,'=1+1" in csv_text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_export_execution_logs_csv_ignores_page_filters(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    execution_log = ExecutionLog(
        rule_id=rule.id,
        trigger_type=TriggerType.MANUAL,
        status=ExecutionStatus.FAILED,
        error_message="csv 全量导出",
    )
    session.add(execution_log)
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/logs/executions.csv?execution_status=success&keyword=missing")

        assert response.status_code == 200
        assert "csv 全量导出" in response.content.decode("utf-8-sig")
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_export_mail_logs_csv(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    execution_log = ExecutionLog(rule_id=rule.id, trigger_type=TriggerType.MANUAL)
    session.add(execution_log)
    session.commit()
    session.refresh(execution_log)
    mail_log = MailLog(
        execution_log_id=execution_log.id,
        recipients="ops@example.com",
        cc_recipients="team@example.com",
        subject="@SUM(A1:A2)",
        status=MailStatus.FAILED,
        error_message="-2+3",
    )
    session.add(mail_log)
    session.commit()
    session.refresh(mail_log)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.get("/logs/mails.csv")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert 'filename="mail-logs.csv"' in response.headers["content-disposition"]
        assert response.content.startswith("\ufeff".encode())
        csv_text = response.content.decode("utf-8-sig")
        assert "ID,执行记录ID,收件人,抄送,主题,状态,错误信息,发送时间" in csv_text
        assert f"{mail_log.id},{execution_log.id},ops@example.com,team@example.com,'@SUM(A1:A2),failed,'-2+3," in csv_text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_export_execution_logs_csv_streams_bounded_batches_after_request_session_closes(
    monkeypatch, session
):
    routes = importlib.import_module("app.routes")
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    session.add_all(
        [
            ExecutionLog(rule_id=rule.id, trigger_type=TriggerType.MANUAL)
            for _ in range(1200)
        ]
    )
    session.commit()
    statements = []

    class NoAllResult:
        def __init__(self, result):
            self.result = result

        def __iter__(self):
            return iter(self.result)

        def all(self):
            raise AssertionError("CSV export must not load all logs at once")

    class TrackingSession(Session):
        def exec(self, statement, *args, **kwargs):
            statements.append(statement)
            return NoAllResult(super().exec(statement, *args, **kwargs))

    monkeypatch.setattr(routes, "Session", TrackingSession)

    response = routes.export_execution_logs_csv(_admin_user(), session)

    assert isinstance(response, StreamingResponse)
    session.close()
    csv_text = run(_read_stream_body(response))
    assert csv_text.startswith("\ufeffID,规则ID,触发方式,状态,")
    assert csv_text.count("\r\n") == 1201
    batch_statements = [
        statement for statement in statements if statement._limit_clause is not None
    ]
    assert len(batch_statements) >= 3
    assert all(statement._limit_clause.value == 500 for statement in batch_statements)


def test_export_execution_logs_csv_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/logs/executions.csv")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_export_mail_logs_csv_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/logs/mails.csv")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_logs_page_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/logs")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_run_rule_requires_login(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = _post_as_unauthenticated(client, "/rules/1/run")

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_run_rule_persists_success_and_redirects(monkeypatch, session):
    import app.execution_service as execution_service

    from app.mailer import MailSendResult
    from app.sql_client import QueryResult

    class FakeSqlClient:
        def query(self, sql, timeout_seconds, max_rows):
            return QueryResult(rows=[{"id": 1, "amount": 100}])

    class FakeMailer:
        def send(self, message):
            return MailSendResult(success=True)

    data_source = _create_data_source(session)
    _create_smtp_config(session)
    rule = _create_rule(session, data_source)
    monkeypatch.setattr(execution_service, "build_sql_client", lambda data_source: FakeSqlClient())
    monkeypatch.setattr(execution_service, "build_smtp_mailer", lambda config: FakeMailer())
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/rules/{rule.id}/run", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/logs"
        execution_log = session.exec(select(ExecutionLog)).one()
        assert execution_log.status == ExecutionStatus.SUCCESS
        assert execution_log.trigger_type == TriggerType.MANUAL
        assert execution_log.row_count == 1
        assert execution_log.email_count == 1
        mail_log = session.exec(select(MailLog)).one()
        assert mail_log.status == MailStatus.SUCCESS
        assert mail_log.subject == "大额订单预警"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_run_rule_returns_conflict_when_execution_lease_is_busy(monkeypatch, session):
    from app.execution_lock import RuleExecutionInProgressError

    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    monkeypatch.setattr(
        "app.routes.execute_rule_by_id",
        Mock(side_effect=RuleExecutionInProgressError("busy")),
    )
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/rules/{rule.id}/run")

        assert response.status_code == 409
        assert "规则正在执行，请稍后重试" in response.text
        assert session.exec(select(ExecutionLog)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_run_rule_missing_smtp_persists_failed_log_without_500(monkeypatch, session):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/rules/{rule.id}/run", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/logs"
        execution_log = session.exec(select(ExecutionLog)).one()
        assert execution_log.status == ExecutionStatus.FAILED
        assert execution_log.error_type == "ConfigurationError"
        assert "SMTP" in execution_log.error_message
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_run_rule_disabled_data_source_persists_failed_log_without_500(monkeypatch, session):
    data_source = _create_data_source(session)
    data_source.enabled = False
    session.add(data_source)
    session.commit()
    rule = _create_rule(session, data_source)
    _create_smtp_config(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(f"/rules/{rule.id}/run", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/logs"
        execution_log = session.exec(select(ExecutionLog)).one()
        assert execution_log.status == ExecutionStatus.FAILED
        assert execution_log.error_type == "ConfigurationError"
        assert "数据源" in execution_log.error_message
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_static_stylesheet_is_mounted(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/static/styles.css")

        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]
    finally:
        get_settings.cache_clear()


def test_static_stylesheet_is_mounted_from_other_cwd(tmp_path, monkeypatch):
    _set_required_settings(monkeypatch)
    monkeypatch.chdir(tmp_path)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.get("/static/styles.css")

        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("query_timeout_seconds", "0", "查询超时必须在 1 至 600 之间"),
        ("max_rows", "5001", "最大行数必须在 1 至 5000 之间"),
    ],
)
def test_rule_forms_reject_out_of_range_query_limits(monkeypatch, session, field, value, message):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        create_form = _valid_rule_form(data_source.id)
        create_form[field] = value
        create_response = client.post("/rules", data=create_form)
        update_form = _valid_rule_form(data_source.id)
        update_form[field] = value
        update_response = client.post(f"/rules/{rule.id}", data=update_form)

        assert create_response.status_code == 400
        assert update_response.status_code == 400
        assert message in create_response.text
        assert message in update_response.text
        assert session.exec(select(AlertRuleVersion)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("query_timeout_seconds", 601, "查询超时时间必须在 1 至 600 之间"),
        ("max_rows", 5001, "最大返回行数必须在 1 至 5000 之间"),
    ],
)
def test_import_rejects_out_of_range_query_limits(monkeypatch, session, field, value, message):
    data_source = _create_data_source(session)
    rule_data = {
        "name": "导入规则",
        "data_source_name": data_source.name,
        "sql_text": "select id from imported_orders",
        "cron_expression": "15 8 * * 1-5",
        "recipients": "ops@example.com",
        "subject_template": "导入预警",
        "body_template": "{{table}}",
        "query_timeout_seconds": 30,
        "max_rows": 500,
    }
    rule_data[field] = value
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/import",
            files={"file": ("rules.json", json.dumps({"version": 1, "rules": [rule_data]}), "application/json")},
        )

        assert response.status_code == 400
        assert message in response.text
        assert session.exec(select(AlertRule)).all() == []
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_import_rejects_oversized_rule_count_and_file(monkeypatch, session):
    data_source = _create_data_source(session)
    rule_data = {
        "name": "导入规则",
        "data_source_name": data_source.name,
        "sql_text": "select id from imported_orders",
        "cron_expression": "15 8 * * 1-5",
        "recipients": "ops@example.com",
        "subject_template": "导入预警",
        "body_template": "{{table}}",
    }
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        too_many_response = client.post(
            "/rules/import",
            files={
                "file": (
                    "rules.json",
                    json.dumps({"version": 1, "rules": [rule_data] * 501}),
                    "application/json",
                )
            },
        )
        too_large_response = client.post(
            "/rules/import",
            files={"file": ("oversized.json", b" " * 1_048_577, "application/json")},
        )

        assert too_many_response.status_code == 400
        assert "导入规则数量不能超过 500 条" in too_many_response.text
        assert too_large_response.status_code == 400
        assert "导入文件不能超过 1 MiB" in too_large_response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("port", "0", "端口必须在 1 至 65535 之间"),
        ("connect_timeout_seconds", "601", "连接超时必须在 1 至 600 之间"),
    ],
)
def test_sql_server_forms_reject_out_of_range_values_without_echoing_password(
    monkeypatch, session, field, value, message
):
    data_source = _create_data_source(session)
    form_data = {
        "name": data_source.name,
        "host": data_source.host,
        "port": "1433",
        "database": data_source.database,
        "username": data_source.username,
        "password": "never-render-this-password",
        "connect_timeout_seconds": "10",
    }
    form_data[field] = value
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        create_response = client.post("/settings/sql-server", data={**form_data, "name": "新生产库"})
        update_response = client.post(f"/settings/sql-server/{data_source.id}", data=form_data)

        assert create_response.status_code == 400
        assert update_response.status_code == 400
        assert message in create_response.text
        assert message in update_response.text
        assert "never-render-this-password" not in create_response.text
        assert "never-render-this-password" not in update_response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_new_sql_server_defaults_to_certificate_validation_and_existing_yes_warns(monkeypatch, session):
    data_source = _create_data_source(session)
    data_source.trust_server_certificate = "yes"
    session.add(data_source)
    session.commit()
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        settings_response = client.get("/settings")
        edit_response = client.get(f"/settings/sql-server/{data_source.id}/edit")

        assert SqlDataSource.model_fields["encrypt"].default == "yes"
        assert SqlDataSource.model_fields["trust_server_certificate"].default == "no"
        assert (
            '<select name="encrypt">\n'
            '              <option value="yes" selected>yes</option>'
        ) in settings_response.text
        assert (
            '<select name="trust_server_certificate">\n'
            '              <option value="yes">yes</option>\n'
            '              <option value="no" selected>no</option>'
        ) in settings_response.text
        assert "证书校验风险" in settings_response.text
        assert '<option value="yes" selected>yes</option>' in edit_response.text
        assert "证书校验风险" in edit_response.text
        session.refresh(data_source)
        assert data_source.trust_server_certificate == "yes"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("kind", "field", "value", "message"),
    [
        ("sql", "port", 0, "端口必须在 1 至 65535 之间"),
        ("sql", "connect_timeout_seconds", 601, "连接超时必须在 1 至 600 之间"),
        ("smtp", "port", 0, "端口必须在 1 至 65535 之间"),
        ("smtp", "timeout_seconds", 601, "SMTP 超时必须在 1 至 600 之间"),
    ],
)
def test_connection_tests_reject_invalid_saved_values(monkeypatch, session, kind, field, value, message):
    routes = importlib.import_module("app.routes")
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        if kind == "sql":
            config = _create_data_source(session)
            path = f"/settings/sql-server/{config.id}/test"
            factory_name = "build_sql_client"
        else:
            config = _create_smtp_config(session)
            path = f"/settings/smtp/{config.id}/test"
            factory_name = "build_smtp_mailer"
        setattr(config, field, value)
        session.add(config)
        session.commit()
        factory = Mock()
        monkeypatch.setattr(routes, factory_name, factory)

        response = client.post(path)

        assert response.status_code == 400
        assert message in response.text
        factory.assert_not_called()
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_sql_validation_and_preview_reject_invalid_saved_connection_timeout(monkeypatch, session):
    data_source = _create_data_source(session)
    data_source.connect_timeout_seconds = 601
    session.add(data_source)
    session.commit()
    routes = importlib.import_module("app.routes")
    build_client = Mock()
    monkeypatch.setattr(routes, "build_sql_client", build_client)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        validate_response = client.post(
            "/rules/validate-sql",
            data={"data_source_id": str(data_source.id), "sql_text": "select 1"},
        )
        preview_response = client.post(
            "/rules/preview-sql",
            data={
                "data_source_id": str(data_source.id),
                "sql_text": "select 1",
                "query_timeout_seconds": "601",
            },
        )

        assert validate_response.status_code == 400
        assert validate_response.json()["message"] == "连接超时必须在 1 至 600 之间"
        assert preview_response.status_code == 400
        assert preview_response.json()["message"] == "查询超时必须在 1 至 600 之间"
        build_client.assert_not_called()
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("port", "0", "端口必须在 1 至 65535 之间"),
        ("timeout_seconds", "601", "SMTP 超时必须在 1 至 600 之间"),
    ],
)
def test_smtp_forms_reject_out_of_range_values_without_echoing_password(
    monkeypatch, session, field, value, message
):
    smtp_config = _create_smtp_config(session)
    form_data = {
        "host": smtp_config.host,
        "port": "587",
        "username": smtp_config.username,
        "password": "never-render-this-password",
        "sender": smtp_config.sender,
        "timeout_seconds": "10",
    }
    form_data[field] = value
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        create_response = client.post("/settings/smtp", data=form_data)
        update_response = client.post(f"/settings/smtp/{smtp_config.id}", data=form_data)

        assert create_response.status_code == 400
        assert update_response.status_code == 400
        assert message in create_response.text
        assert message in update_response.text
        assert "never-render-this-password" not in create_response.text
        assert "never-render-this-password" not in update_response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("query_timeout_seconds", "查询超时必须是整数"),
        ("max_rows", "最大行数必须是整数"),
    ],
)
def test_rule_forms_reject_non_numeric_query_limits_with_actionable_400(
    monkeypatch, session, field, message
):
    data_source = _create_data_source(session)
    rule = _create_rule(session, data_source)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        create_form = _valid_rule_form(data_source.id)
        create_form[field] = "abc"
        update_form = _valid_rule_form(data_source.id)
        update_form[field] = "abc"

        create_response = client.post("/rules", data=create_form)
        update_response = client.post(f"/rules/{rule.id}", data=update_form)

        assert create_response.status_code == 400
        assert update_response.status_code == 400
        assert message in create_response.text
        assert message in update_response.text
        assert "422 Unprocessable Entity" not in create_response.text
        assert "422 Unprocessable Entity" not in update_response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_preview_rejects_non_numeric_query_timeout_with_actionable_400(monkeypatch, session):
    data_source = _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post(
            "/rules/preview-sql",
            data={
                "data_source_id": str(data_source.id),
                "sql_text": "select 1",
                "query_timeout_seconds": "abc",
            },
        )

        assert response.status_code == 400
        assert response.json() == {"success": False, "message": "查询超时必须是整数"}
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("port", "端口必须是整数"),
        ("connect_timeout_seconds", "连接超时必须是整数"),
    ],
)
def test_sql_server_forms_reject_non_numeric_values_with_safe_400(
    monkeypatch, session, field, message
):
    data_source = _create_data_source(session)
    form_data = {
        "name": data_source.name,
        "host": data_source.host,
        "port": "1433",
        "database": data_source.database,
        "username": data_source.username,
        "password": "never-render-this-password",
        "connect_timeout_seconds": "10",
    }
    form_data[field] = "abc"
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        create_response = client.post("/settings/sql-server", data={**form_data, "name": "新生产库"})
        update_response = client.post(f"/settings/sql-server/{data_source.id}", data=form_data)

        assert create_response.status_code == 400
        assert update_response.status_code == 400
        assert message in create_response.text
        assert message in update_response.text
        assert 'value="abc"' in create_response.text
        assert 'value="abc"' in update_response.text
        assert "never-render-this-password" not in create_response.text
        assert "never-render-this-password" not in update_response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("port", "端口必须是整数"),
        ("timeout_seconds", "SMTP 超时必须是整数"),
    ],
)
def test_smtp_forms_reject_non_numeric_values_with_safe_400(monkeypatch, session, field, message):
    smtp_config = _create_smtp_config(session)
    form_data = {
        "host": smtp_config.host,
        "port": "587",
        "username": smtp_config.username,
        "password": "never-render-this-password",
        "sender": smtp_config.sender,
        "timeout_seconds": "10",
    }
    form_data[field] = "abc"
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        create_response = client.post("/settings/smtp", data=form_data)
        update_response = client.post(f"/settings/smtp/{smtp_config.id}", data=form_data)

        assert create_response.status_code == 400
        assert update_response.status_code == 400
        assert message in create_response.text
        assert message in update_response.text
        assert 'value="abc"' in create_response.text
        assert 'value="abc"' in update_response.text
        assert "never-render-this-password" not in create_response.text
        assert "never-render-this-password" not in update_response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

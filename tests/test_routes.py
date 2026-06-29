import importlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlmodel import select

from app.models import (
    AdminUser,
    AlertRule,
    ExecutionLog,
    ExecutionStatus,
    MailLog,
    MailStatus,
    SendMode,
    SmtpConfig,
    SqlDataSource,
    TriggerType,
)

VALID_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def _set_required_settings(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
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
    return TestClient(app), get_settings, app


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
        Settings(session_secret="valid-session-secret", secret_key="not-a-fernet-key")

    error_fields = {error["loc"][0] for error in exc_info.value.errors()}
    assert "secret_key" in error_fields


def test_settings_reads_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_NAME", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    tmp_path.joinpath(".env").write_text(
        "\n".join(
            [
                "APP_NAME=Env 文件预警系统",
                "SESSION_SECRET=dotenv-session-secret",
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
        assert settings.session_secret == "dotenv-session-secret"
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

        response = client.post("/rules", data={"name": "x"})

        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_create_rule_persists_alert_rule(monkeypatch, session):
    data_source = _create_data_source(session)
    client, get_settings, app = _client_with_admin(monkeypatch, session)
    try:
        response = client.post("/rules", data=_valid_rule_form(data_source.id))

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
    finally:
        app.dependency_overrides.clear()
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
        assert "smtp.example.com" in response.text
        assert "encrypted" not in response.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_create_sql_server_settings_requires_admin_session(monkeypatch):
    _set_required_settings(monkeypatch)
    create_app, get_settings = _load_create_app()
    try:
        client = TestClient(create_app())

        response = client.post("/settings/sql-server", data={"name": "生产库"})

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
            },
        )

        assert response.status_code == 303
        data_source = session.exec(select(SqlDataSource)).one()
        assert data_source.encrypted_password != "plain-password"
        assert data_source.connect_timeout_seconds == 15
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
        )

        assert response.status_code == 303
        smtp_config = session.exec(select(SmtpConfig)).one()
        assert smtp_config.encrypted_password != "smtp-password"
        assert smtp_config.use_tls is True
        assert smtp_config.use_ssl is False
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
        assert "大额订单预警" in response.text
        assert "ops@example.com" in response.text
        assert "manual" in response.text
    finally:
        app.dependency_overrides.clear()
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

        response = client.post("/rules/1/run")

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

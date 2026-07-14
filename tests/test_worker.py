from unittest.mock import Mock

from sqlmodel import Session

from app.models import AlertRule, SendMode, SqlDataSource, utc_now
from app.worker import sync_rules_once


def _persist_rule(session, data_source_id, *, name, archived_at=None):
    rule = AlertRule(
        name=name,
        data_source_id=data_source_id,
        sql_text="select id from orders",
        cron_expression="0 9 * * *",
        recipients="ops@example.com",
        subject_template="预警",
        body_template="{{table}}",
        send_mode=SendMode.SUMMARY,
        archived_at=archived_at,
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


def test_sync_rules_once_passes_only_active_rules_to_synchronizer(engine):
    with Session(engine) as session:
        data_source = SqlDataSource(
            name="prod",
            host="db.example.com",
            database="erp",
            username="readonly",
            encrypted_password="encrypted",
        )
        session.add(data_source)
        session.commit()
        session.refresh(data_source)
        active_rule = _persist_rule(session, data_source.id, name="active")
        active_rule_id = active_rule.id
        _persist_rule(session, data_source.id, name="archived", archived_at=utc_now())

    synchronizer = Mock()

    result = sync_rules_once(synchronizer, session_factory=lambda: Session(engine))

    assert result is True
    synced_rules = synchronizer.sync.call_args.args[0]
    assert [rule.id for rule in synced_rules] == [active_rule_id]

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from app.models import RuleExecutionLease, utc_now


class RuleExecutionInProgressError(Exception):
    pass


def _release_rule_execution_lease(
    session: Session,
    rule_id: int,
    owner_token: str,
) -> None:
    try:
        session.rollback()
        session.execute(
            delete(RuleExecutionLease).where(
                RuleExecutionLease.rule_id == rule_id,
                RuleExecutionLease.owner_token == owner_token,
            )
        )
        session.commit()
    except BaseException:
        session.rollback()
        raise


@contextmanager
def rule_execution_lease(
    session: Session,
    rule_id: int,
    *,
    lease_seconds: int,
    now_fn: Callable[[], datetime] = utc_now,
) -> Iterator[None]:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")

    acquired_at = now_fn()
    owner_token = uuid4().hex
    expires_at = acquired_at + timedelta(seconds=lease_seconds)
    statement = sqlite_insert(RuleExecutionLease).values(
        rule_id=rule_id,
        owner_token=owner_token,
        acquired_at=acquired_at,
        expires_at=expires_at,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[RuleExecutionLease.rule_id],
        set_={
            "owner_token": owner_token,
            "acquired_at": acquired_at,
            "expires_at": expires_at,
        },
        where=RuleExecutionLease.expires_at <= acquired_at,
    )

    try:
        result = session.execute(statement)
        session.commit()
    except Exception:
        session.rollback()
        raise
    if result.rowcount != 1:
        raise RuleExecutionInProgressError(f"rule {rule_id} is already running")

    try:
        yield
    except BaseException:
        try:
            _release_rule_execution_lease(session, rule_id, owner_token)
        except BaseException:
            pass
        raise
    else:
        _release_rule_execution_lease(session, rule_id, owner_token)

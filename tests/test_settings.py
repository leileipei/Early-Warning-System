from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.models import utc_now
from app.settings import Settings


def valid_settings_payload() -> dict[str, object]:
    return {
        "session_secret": "s" * 32,
        "secret_key": Fernet.generate_key().decode(),
    }


def test_session_secret_requires_at_least_32_bytes():
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        Settings(**valid_settings_payload() | {"session_secret": "短" * 10})


def test_scheduler_misfire_grace_seconds_rejects_bool():
    with pytest.raises(ValidationError):
        Settings(**valid_settings_payload() | {"scheduler_misfire_grace_seconds": True})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scheduler_misfire_grace_seconds", 0),
        ("worker_heartbeat_timeout_seconds", 0),
        ("log_retention_days", 0),
        ("log_cleanup_interval_seconds", 0),
        ("session_max_age_seconds", 0),
        ("session_idle_timeout_seconds", 0),
    ],
)
def test_positive_production_settings(field, value):
    with pytest.raises(ValidationError):
        Settings(**valid_settings_payload() | {field: value})


def test_utc_now_returns_naive_utc_datetime():
    value = utc_now()

    assert value.tzinfo is None
    assert abs((datetime.now(UTC).replace(tzinfo=None) - value).total_seconds()) < 1

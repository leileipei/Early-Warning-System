from sqlmodel import select

from app.admin_cli import upsert_admin_user
from app.models import AdminUser
from app.security import verify_password


def test_password_compatibility_contract_for_admin_cli(session):
    legacy_hash = "$2b$12$GSm13057BqwXHr3/6MpZLeV9aTcFcj2VpuH5UBb1Q.OZE0DbRIoa."

    user = upsert_admin_user(session, "admin", "new-password")

    assert verify_password("legacy-password", legacy_hash)
    assert not verify_password("new-password", "not-a-bcrypt-hash")
    assert verify_password("new-password", user.password_hash)


def test_upsert_admin_user_creates_admin(session):
    user = upsert_admin_user(session, "admin", "initial-password")

    saved = session.exec(select(AdminUser).where(AdminUser.username == "admin")).one()
    assert saved.id == user.id
    assert verify_password("initial-password", saved.password_hash)


def test_upsert_admin_user_updates_existing_password(session):
    first = upsert_admin_user(session, "admin", "initial-password")

    second = upsert_admin_user(session, "admin", "new-password")

    users = session.exec(select(AdminUser)).all()
    assert len(users) == 1
    assert second.id == first.id
    assert verify_password("new-password", users[0].password_hash)
    assert not verify_password("initial-password", users[0].password_hash)

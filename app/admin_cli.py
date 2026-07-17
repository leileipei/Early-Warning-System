import argparse
import getpass

from sqlmodel import Session, select

from app.db import get_engine, init_db
from app.models import AdminUser
from app.security import hash_password


def upsert_admin_user(session: Session, username: str, password: str) -> AdminUser:
    user = session.exec(select(AdminUser).where(AdminUser.username == username)).first()
    password_hash = hash_password(password)
    if user is None:
        user = AdminUser(username=username, password_hash=password_hash)
        session.add(user)
    else:
        user.password_hash = password_hash
        user.session_version += 1
        session.add(user)
    session.commit()
    session.refresh(user)
    return user


def main() -> None:
    parser = argparse.ArgumentParser(description="创建或更新后台管理员账号")
    parser.add_argument("username", help="管理员用户名")
    parser.add_argument("--password", help="管理员密码；未提供时会交互输入")
    args = parser.parse_args()

    password = args.password or getpass.getpass("管理员密码: ")
    if not password:
        raise SystemExit("管理员密码不能为空")

    init_db()
    with Session(get_engine()) as session:
        user = upsert_admin_user(session, args.username, password)
    print(f"管理员账号已保存: {user.username}")


if __name__ == "__main__":
    main()

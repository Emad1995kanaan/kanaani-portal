"""Create (or reset) an administrator account.

    python -m scripts.create_admin --username admin
    # prompts for the password (hidden); or set ADMIN_PASSWORD in the environment for automation.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import ROLE_ADMIN, User
from app.security import hash_password, password_problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Create or reset an admin account.")
    ap.add_argument("--username", required=True)
    ap.add_argument("--display-name", default="Administrator")
    ap.add_argument("--reset", action="store_true", help="Reset the password if the admin already exists.")
    a = ap.parse_args()
    pw = os.getenv("ADMIN_PASSWORD") or getpass.getpass("New admin password: ")
    if not os.getenv("ADMIN_PASSWORD") and pw != getpass.getpass("Repeat password: "):
        print("Passwords don't match.", file=sys.stderr)
        return 1
    problems = password_problems(pw)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    with SessionLocal() as db:
        u = db.scalar(select(User).where(func.lower(User.username) == a.username.lower()))
        if u and not a.reset:
            print(f"User '{a.username}' already exists. Use --reset to set a new password.", file=sys.stderr)
            return 1
        if u and u.role != ROLE_ADMIN:
            print(f"'{a.username}' is a reseller account, not an admin.", file=sys.stderr)
            return 1
        if u is None:
            u = User(username=a.username, role=ROLE_ADMIN, display_name=a.display_name, password_hash="")
            db.add(u)
        u.password_hash = hash_password(pw)
        u.is_active = True
        u.must_change_password = False
        db.commit()
    print(f"Admin '{a.username}' is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

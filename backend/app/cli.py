import argparse
import getpass
import json
import os
from pathlib import Path
from sqlalchemy import select
from .builtin import ensure_builtin
from .db import SessionLocal
from .models import User
from .security import hash_password

def main():
    parser = argparse.ArgumentParser(description="Paper Reader administration")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("init-admin")
    admin.add_argument("--username", required=True)
    schema = sub.add_parser("export-openapi")
    schema.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "export-openapi":
        from .main import app
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n")
        print(f"OpenAPI exported to {target}")
        return
    password = os.environ.get("PAPER_READER_ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    if len(password) < 10:
        parser.error("Admin password must contain at least 10 characters")
    if not args.username.strip() or len(args.username) > 80:
        parser.error("Username must contain 1–80 characters")
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user and not user.is_admin:
            parser.error("Existing account is not an administrator; refusing to elevate it")
        if not user:
            db.add(User(username=args.username, password_hash=hash_password(password), is_admin=True))
        ensure_builtin(db)
        db.commit()
    print("Administrator and built-in template are initialized. Existing credentials were preserved.")

if __name__ == "__main__":
    main()

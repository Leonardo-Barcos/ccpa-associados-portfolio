"""Create the first administrator of the management panel — or rescue one.

    python scripts/criar_admin.py

Every other account is created from inside the panel (Usuários). This script
exists for the two moments the panel cannot help: before anyone can sign in,
and when the only administrator has lost their password.

It writes to the database named by DATABASE_URL, so point it at production:

    $env:DATABASE_URL = "postgresql://...-pooler.../neondb?sslmode=require"

If the e-mail already has an account, it is reactivated, made an
administrator and given the new password.
"""

import getpass
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "postgres" in os.getenv("DATABASE_URL", "") and not importlib.util.find_spec(
    "psycopg"
):
    print("O driver do PostgreSQL não está instalado neste computador.")
    print('Rode:  pip install "psycopg[binary]"')
    raise SystemExit(1)

from sqlalchemy import func, inspect, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.models import AdminRole, AdminUser, AuditLog  # noqa: E402
from app.security import hash_password  # noqa: E402

MIN_PASSWORD = 10


def ask(prompt: str) -> str:
    value = input(prompt).strip()
    if not value:
        print("Campo obrigatório.")
        raise SystemExit(1)
    return value


def main() -> None:
    host = settings.database_url.split("@")[-1].split("/")[0]
    print(f"\n  Banco: {engine.dialect.name} em {host}\n")

    if "admin_users" not in inspect(engine).get_table_names():
        print("  A tabela do painel ainda não existe neste banco.")
        print("  Rode antes:  python scripts/init_db.py\n")
        raise SystemExit(1)

    name = ask("  Nome: ")
    email = ask("  E-mail: ").lower()
    password = getpass.getpass("  Senha (não aparece enquanto digita): ")
    if len(password) < MIN_PASSWORD:
        print(f"  A senha precisa de pelo menos {MIN_PASSWORD} caracteres.")
        raise SystemExit(1)
    if getpass.getpass("  Repita a senha: ") != password:
        print("  As senhas não conferem.")
        raise SystemExit(1)

    with Session(engine) as db:
        user = db.scalar(select(AdminUser).where(func.lower(AdminUser.email) == email))
        if user:
            user.name = name
            user.role = AdminRole.ADMIN
            user.active = True
            user.password_hash = hash_password(password)
            user.must_change_password = False
            user.session_version += 1
            action, verb = "redefiniu_senha", "atualizado"
        else:
            user = AdminUser(
                name=name,
                email=email,
                role=AdminRole.ADMIN,
                password_hash=hash_password(password),
                # They typed it themselves, so there is nothing to replace.
                must_change_password=False,
            )
            db.add(user)
            action, verb = "criou_usuario", "criado"
        db.flush()
        db.add(
            AuditLog(
                user_id=None,
                user_name="scripts/criar_admin.py",
                action=action,
                target=user.id,
                detail=f"{name} <{email}> · ADMIN",
            )
        )
        db.commit()

    print(f"\n  Administrador {verb}: {name} <{email}>")
    print(f"  Entre em {settings.base_url.rstrip('/')}/painel\n")


if __name__ == "__main__":
    main()

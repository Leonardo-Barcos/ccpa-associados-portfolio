"""Create the tables in the database named by DATABASE_URL.

Run once, against the production database, before the first deploy:

    python scripts/init_db.py

The application does not do this on boot in production (AUTO_CREATE_TABLES is
off there), because on a serverless platform every cold start would run it
again on a database that already has the tables.

Existing tables are left alone: SQLAlchemy only creates what is missing, and
never alters or drops a column. A change to an existing table is a migration,
and needs a migration tool.
"""

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The Postgres driver is skipped on Windows by a marker in requirements.txt,
# because development there runs on SQLite. Pointing this script at a hosted
# Postgres is the one time a Windows machine needs it, and the import error
# SQLAlchemy raises names psycopg without saying how to get it.
if "postgres" in os.getenv("DATABASE_URL", "") and not importlib.util.find_spec(
    "psycopg"
):
    print("O driver do PostgreSQL não está instalado neste computador.")
    print('Rode:  pip install "psycopg[binary]"')
    raise SystemExit(1)

from sqlalchemy import inspect  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, engine, ensure_schema, init_db  # noqa: E402


def main() -> None:
    # The URL carries the password, so only the host is safe to print.
    host = settings.database_url.split("@")[-1].split("/")[0]
    print(f"Banco: {engine.dialect.name} em {host}")

    before = set(inspect(engine).get_table_names())
    init_db()
    after = set(inspect(engine).get_table_names())

    created = sorted(after - before)
    added = ensure_schema()
    if created:
        print("Tabelas criadas: " + ", ".join(created))
    if added:
        print("Tabelas/colunas acrescentadas: " + ", ".join(added))
    if not created and not added:
        print("Nada a fazer — tabelas e colunas já existem.")

    missing = sorted(set(Base.metadata.tables) - after)
    if missing:
        print("ATENÇÃO — não foram criadas: " + ", ".join(missing))
        raise SystemExit(1)

    print("Pronto. Em produção, deixe AUTO_CREATE_TABLES=false.")


if __name__ == "__main__":
    main()

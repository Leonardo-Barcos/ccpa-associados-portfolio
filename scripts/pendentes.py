"""List the people who signed up and have not paid yet.

    python scripts/pendentes.py

Without e-mail configured, nothing tells the association that somebody signed
up and stopped at the card screen: there is no welcome message and no copy to
the inbox. This is the replacement — run it, and follow up by WhatsApp with the
payment link it prints.

Reads the database named by DATABASE_URL, so point it at production:

    $env:DATABASE_URL = "postgresql://...-pooler.../neondb?sslmode=require"
"""

import importlib.util
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "postgres" in os.getenv("DATABASE_URL", "") and not importlib.util.find_spec(
    "psycopg"
):
    print("O driver do PostgreSQL não está instalado neste computador.")
    print('Rode:  pip install "psycopg[binary]"')
    raise SystemExit(1)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import engine  # noqa: E402
from app.models import Member, MemberStatus, Payment  # noqa: E402
from app.plans import PLANS  # noqa: E402


def _idade(momento: datetime) -> str:
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=UTC)
    horas = (datetime.now(UTC) - momento).total_seconds() / 3600
    if horas < 1:
        return "agora há pouco"
    if horas < 24:
        return f"há {int(horas)}h"
    return f"há {int(horas / 24)} dia(s)"


def main() -> None:
    with Session(engine) as db:
        pendentes = db.scalars(
            select(Member)
            .where(Member.status == MemberStatus.AWAITING_PAYMENT)
            .order_by(Member.created_at.desc())
        ).all()

        if not pendentes:
            print("\n  Ninguém pendente. Todo mundo que se cadastrou já pagou.\n")
            return

        print(f"\n  {len(pendentes)} cadastro(s) aguardando pagamento\n")

        for member in pendentes:
            plan = PLANS.get(member.plan_code)
            payment = db.scalars(
                select(Payment)
                .where(Payment.member_id == member.id)
                .order_by(Payment.created_at.desc())
            ).first()

            print(f"  {member.trade_name or member.name}")
            print(
                f"      protocolo   {member.id[:8].upper()}  ·  {_idade(member.created_at)}"
            )
            print(f"      plano       {plan.name if plan else member.plan_code}", end="")
            if plan:
                print(f"  ·  R$ {plan.amount_brl}{plan.cycle_label}")
            else:
                print()
            if member.mobile:
                print(f"      whatsapp    {member.mobile}")
            print(f"      e-mail      {member.email}")
            if payment and payment.checkout_url:
                print(f"      pagamento   {payment.checkout_url}")
            else:
                print("      pagamento   SEM LINK — a cobrança não chegou a ser criada")
            print()

        print("  Mande o link de pagamento pelo WhatsApp de quem ainda não pagou.")
        print("  Quem estiver sem link precisa se cadastrar de novo.\n")


if __name__ == "__main__":
    main()

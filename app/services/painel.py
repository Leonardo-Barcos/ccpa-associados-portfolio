"""Queries and figures behind the management panel.

Kept out of the router so the numbers can be tested without HTTP, and so the
list, the CSV export and the counters all read members through the very same
filter — a spreadsheet that disagrees with the screen it was exported from is
worse than no spreadsheet.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote, urlencode

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    AdminUser,
    AuditLog,
    Charge,
    EntityType,
    Member,
    MemberStatus,
    Payment,
)
from ..plans import MONTHLY_PLANS, PLANS
from ..security import client_ip
from ..templating import TZ
from ..validators import normalize_cnpj
from .billing import PAID

STATUS_LABELS = {
    MemberStatus.ACTIVE: "Ativo",
    MemberStatus.AWAITING_PAYMENT: "Aguardando pagamento",
    MemberStatus.OVERDUE: "Inadimplente",
    MemberStatus.CANCELLED: "Cancelado",
}

# Tone of each status badge — one of the brand's colours plus a neutral.
STATUS_TONES = {
    MemberStatus.ACTIVE: "verde",
    MemberStatus.AWAITING_PAYMENT: "amarelo",
    MemberStatus.OVERDUE: "vermelho",
    MemberStatus.CANCELLED: "neutro",
}

MONTHS = ("jan", "fev", "mar", "abr", "mai", "jun",
          "jul", "ago", "set", "out", "nov", "dez")  # fmt: skip

PAGE_SIZE = 25


# --------------------------------------------------------------------------
#  Time
# --------------------------------------------------------------------------
def local(moment: datetime | None) -> datetime | None:
    """A stored instant, in Belém time.

    SQLite hands timezone-aware columns back naive; they were written in UTC.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(TZ)


def today() -> date:
    return datetime.now(TZ).date()


def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(day: date, months: int) -> date:
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def last_months(count: int = 12) -> list[date]:
    first = add_months(month_start(today()), -(count - 1))
    return [add_months(first, i) for i in range(count)]


def utc_from_local_day(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=TZ).astimezone(UTC)


def brl(cents: int | float) -> str:
    text = f"{cents / 100:,.2f}"
    return "R$ " + text.replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------------
#  Filters — shared by the list, the export and the counters
# --------------------------------------------------------------------------
TIERS = tuple((code, plan.name) for code, plan in MONTHLY_PLANS.items())


@dataclass
class Filters:
    q: str = ""
    status: str = ""
    plano: str = ""
    ciclo: str = ""
    tipo: str = ""
    regional: str = ""
    desde: str = ""
    ate: str = ""
    pagina: int = 1

    @classmethod
    def from_query(cls, params) -> Filters:
        try:
            page = max(1, int(params.get("pagina") or 1))
        except ValueError:
            page = 1
        return cls(
            q=(params.get("q") or "").strip()[:100],
            status=params.get("status") or "",
            plano=params.get("plano") or "",
            ciclo=params.get("ciclo") or "",
            tipo=params.get("tipo") or "",
            regional=params.get("regional") or "",
            desde=params.get("desde") or "",
            ate=params.get("ate") or "",
            pagina=page,
        )

    @property
    def active(self) -> bool:
        return any(
            (self.q, self.status, self.plano, self.ciclo, self.tipo,
             self.regional, self.desde, self.ate)
        )  # fmt: skip

    def query_string(self, **overrides) -> str:
        values = {
            "q": self.q,
            "status": self.status,
            "plano": self.plano,
            "ciclo": self.ciclo,
            "tipo": self.tipo,
            "regional": self.regional,
            "desde": self.desde,
            "ate": self.ate,
        }
        values.update(overrides)
        return urlencode({k: v for k, v in values.items() if v})


def _parse_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def apply_filters(query: Select, f: Filters) -> Select:
    if f.q:
        like = f"%{f.q}%"
        digits = "".join(ch for ch in f.q if ch.isdigit())
        conditions = [
            Member.name.ilike(like),
            Member.trade_name.ilike(like),
            Member.email.ilike(like),
            Member.login.ilike(like),
            Member.company.ilike(like),
        ]
        # CPF, CNPJ and phone are stored as digits only; searching
        # "111.444.777-35" has to find them just the same.
        if len(digits) >= 3:
            conditions.append(Member.mobile.contains(digits))
        # CPF and CNPJ are stored without punctuation, a new-format CNPJ with
        # upper-case letters: "12.ABC.345/01DE-35" must find "12ABC34501DE35".
        document = normalize_cnpj(f.q)
        if len(document) >= 3:
            conditions.append(Member.document.contains(document))
        query = query.where(or_(*conditions))

    if f.status in MemberStatus.__members__:
        query = query.where(Member.status == MemberStatus[f.status])

    if f.plano in MONTHLY_PLANS:
        query = query.where(Member.plan_code.in_([f.plano, f"{f.plano}_ANUAL"]))

    if f.ciclo == "MONTHLY":
        query = query.where(Member.plan_code.in_(list(MONTHLY_PLANS)))
    elif f.ciclo == "YEARLY":
        query = query.where(Member.plan_code.like("%\\_ANUAL", escape="\\"))

    if f.tipo in EntityType.__members__:
        query = query.where(Member.entity_type == EntityType[f.tipo])

    if f.regional:
        query = query.where(Member.chapter == f.regional)

    since = _parse_day(f.desde)
    if since:
        query = query.where(Member.created_at >= utc_from_local_day(since))
    until = _parse_day(f.ate)
    if until:
        query = query.where(
            Member.created_at < utc_from_local_day(until + timedelta(days=1))
        )
    return query


def list_members(db: Session, f: Filters) -> tuple[list[Member], int]:
    base = apply_filters(select(Member), f)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(Member.created_at.desc())
        .offset((f.pagina - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    return list(rows), total


# --------------------------------------------------------------------------
#  Overview figures
# --------------------------------------------------------------------------
def monthly_value_cents(plan_code: str) -> float:
    """What a plan is worth per month — annual plans spread over twelve."""
    plan = PLANS.get(plan_code)
    if plan is None:
        return 0
    return plan.amount_cents / 12 if plan.is_yearly else plan.amount_cents


def nice_ceiling(value: float) -> float:
    """The next round number at or above `value` (1, 1.5, 2, 2.5, 3, 4, 5,
    6, 8 x 10^n).

    The axis tops out on a number a person would write, instead of on
    whatever the biggest month happened to be.
    """
    if value <= 0:
        return 0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def _bar_series(labels: list[str], values: list[float], money: bool) -> dict:
    def fmt(v: float) -> str:
        return brl(v) if money else str(int(v))

    def fmt_axis(v: float) -> str:
        # Axis ticks are rounded reais: "R$ 1.500", not "R$ 1.500,00".
        if not money:
            return str(int(v))
        return "R$ " + f"{v / 100:,.0f}".replace(",", ".")

    peak = max(values) if values else 0
    top = nice_ceiling(peak) if not money else nice_ceiling(peak / 100) * 100
    if not money and top:
        # Whole people: the middle tick must be a whole number too.
        top = max(4, top + (top % 2))
    peak_index = values.index(peak) if peak else None
    return {
        "bars": [
            {
                "label": label,
                "value": value,
                "text": fmt(value),
                "ratio": (value / top) if top else 0,
                # Selective labels: the current month and the best month.
                "labelled": bool(value) and i in (len(values) - 1, peak_index),
            }
            for i, (label, value) in enumerate(zip(labels, values, strict=True))
        ],
        "ticks": [fmt_axis(top), fmt_axis(top / 2), fmt_axis(0)] if top else [],
        "total_text": fmt(sum(values)),
        "empty": not any(values),
    }


def overview(db: Session) -> dict:
    counts = dict(
        db.execute(select(Member.status, func.count()).group_by(Member.status)).all()
    )

    active_by_plan = dict(
        db.execute(
            select(Member.plan_code, func.count())
            .where(Member.status == MemberStatus.ACTIVE)
            .group_by(Member.plan_code)
        ).all()
    )
    recurring = sum(monthly_value_cents(code) * n for code, n in active_by_plan.items())

    # Revenue: every paid invoice, bucketed by the month it was paid in Belém.
    months = last_months(12)
    window_start = utc_from_local_day(months[0])
    paid_rows = db.execute(
        select(Charge.paid_at, Charge.amount_cents).where(
            Charge.status.in_(PAID), Charge.paid_at >= window_start
        )
    ).all()
    revenue = dict.fromkeys(months, 0)
    for paid_at, amount in paid_rows:
        key = month_start(local(paid_at).date())
        if key in revenue:
            revenue[key] += amount

    signup_rows = db.scalars(
        select(Member.created_at).where(Member.created_at >= window_start)
    ).all()
    signups = dict.fromkeys(months, 0)
    for created_at in signup_rows:
        key = month_start(local(created_at).date())
        if key in signups:
            signups[key] += 1

    labels = [f"{MONTHS[m.month - 1]}/{str(m.year)[2:]}" for m in months]
    this_month, last_month = months[-1], months[-2]

    # Conversion: of everyone who signed up, how many ever paid.
    total_members = sum(counts.values())
    ever_paid = (
        db.scalar(
            select(func.count(func.distinct(Payment.member_id))).where(
                Payment.paid_at.is_not(None)
            )
        )
        or 0
    )

    plan_rows = []
    for code, plan in MONTHLY_PLANS.items():
        monthly = active_by_plan.get(code, 0)
        yearly = active_by_plan.get(f"{code}_ANUAL", 0)
        plan_rows.append((plan.name, monthly, yearly))
    peak_plan = max((m + y for _, m, y in plan_rows), default=0)

    chapter_rows = db.execute(
        select(Member.chapter, func.count())
        .where(Member.status == MemberStatus.ACTIVE)
        .group_by(Member.chapter)
        .order_by(func.count().desc())
    ).all()
    peak_chapter = max((n for _, n in chapter_rows), default=0)

    return {
        "counts": {
            "ativos": counts.get(MemberStatus.ACTIVE, 0),
            "aguardando": counts.get(MemberStatus.AWAITING_PAYMENT, 0),
            "inadimplentes": counts.get(MemberStatus.OVERDUE, 0),
            "cancelados": counts.get(MemberStatus.CANCELLED, 0),
            "total": total_members,
        },
        "recorrente": brl(recurring),
        "recebido_mes": brl(revenue[this_month]),
        "recebido_mes_anterior": brl(revenue[last_month]),
        "variacao": _variation(revenue[this_month], revenue[last_month]),
        "conversao": (
            f"{round(100 * ever_paid / total_members)}%" if total_members else "—"
        ),
        "pagantes": ever_paid,
        "receita": _bar_series(labels, [revenue[m] for m in months], money=True),
        "cadastros": _bar_series(labels, [signups[m] for m in months], money=False),
        "planos": [
            {
                "name": name,
                "monthly": m,
                "yearly": y,
                "total": m + y,
                "ratio_m": m / peak_plan if peak_plan else 0,
                "ratio_y": y / peak_plan if peak_plan else 0,
            }
            for name, m, y in plan_rows
        ],
        "regionais": [
            {
                "name": chapter or "Não informada",
                "total": n,
                "ratio": n / peak_chapter if peak_chapter else 0,
            }
            for chapter, n in chapter_rows
        ],
    }


def _variation(current: int, previous: int) -> str | None:
    if not previous:
        return None
    change = round(100 * (current - previous) / previous)
    return f"{'+' if change > 0 else ''}{change}% sobre o mês anterior"


def needs_attention(db: Session, limit: int = 8) -> list[Member]:
    """Signed up more than a day ago and never paid, or fell behind."""
    cutoff = datetime.now(UTC) - timedelta(days=1)
    return list(
        db.scalars(
            select(Member)
            .where(
                or_(
                    Member.status == MemberStatus.OVERDUE,
                    (Member.status == MemberStatus.AWAITING_PAYMENT)
                    & (Member.created_at < cutoff),
                )
            )
            .order_by(Member.created_at.desc())
            .limit(limit)
        )
    )


def latest_payment(db: Session, member: Member) -> Payment | None:
    return db.scalars(
        select(Payment)
        .where(Payment.member_id == member.id)
        .order_by(Payment.created_at.desc())
    ).first()


# --------------------------------------------------------------------------
#  WhatsApp
# --------------------------------------------------------------------------
def whatsapp_url(member: Member, text: str) -> str | None:
    digits = "".join(ch for ch in (member.mobile or "") if ch.isdigit())
    if len(digits) not in (10, 11):
        return None
    return f"https://wa.me/55{digits}?text={quote(text)}"


def whatsapp_message(member: Member, payment: Payment | None, kind: str) -> str:
    plan = PLANS.get(member.plan_code)
    plan_name = plan.name if plan else member.plan_code
    link = payment.checkout_url if payment else None
    hello = f"Olá, {member.first_name}! Aqui é do Clube de Criativos do Pará."

    pix = bool(payment and payment.is_pix)
    pix_invoice = bool(payment and payment.is_pix_invoice)
    if kind == "cobranca" and member.status == MemberStatus.OVERDUE:
        if pix:
            reason = "Pode ter faltado saldo no dia do débito do Pix Automático."
        elif pix_invoice:
            reason = "Pode ser que o Pix deste período ainda não tenha sido pago."
        else:
            reason = "Pode ter sido o cartão recusado ou vencido."
        text = (
            f"{hello} Notamos que a {plan.charge_noun if plan else 'mensalidade'} "
            f"da sua associação no plano {plan_name} não foi confirmada. {reason}"
        )
        if link:
            text += f" Para regularizar, é só acessar: {link}"
        return text + " Qualquer dúvida, estamos por aqui."

    if kind == "cobranca":
        text = (
            f"{hello} Vimos que você fez o cadastro no plano {plan_name}, "
            "mas o pagamento ainda não foi concluído."
        )
        if link and pix:
            text += (
                f" Para finalizar, é só pagar o Pix por aqui (se o código tiver "
                f"vencido, a página gera um novo): {link}"
            )
        elif link and pix_invoice:
            text += f" Para finalizar, é só pagar com Pix por este link: {link}"
        elif link:
            text += f" Para finalizar sua associação, é só acessar: {link}"
        return text + " Se tiver alguma dificuldade, é só responder esta mensagem."

    return f"{hello} "


# --------------------------------------------------------------------------
#  CSV export
# --------------------------------------------------------------------------
EXPORT_COLUMNS = (
    ("Cadastro", lambda m: _fmt_dt(m.created_at)),
    ("Situação", lambda m: STATUS_LABELS.get(m.status, m.status)),
    ("Tipo", lambda m: "Empresa" if m.entity_type == EntityType.PJ else "Pessoa"),
    ("Plano", lambda m: PLANS[m.plan_code].name if m.plan_code in PLANS else ""),
    ("Ciclo", lambda m: "Anual" if m.plan_code.endswith("_ANUAL") else "Mensal"),
    ("Valor", lambda m: PLANS[m.plan_code].amount_brl if m.plan_code in PLANS else ""),
    ("Pagamento", lambda m: _method_label(m)),
    ("Nome / Razão social", lambda m: m.name),
    ("Nome fantasia", lambda m: m.trade_name or ""),
    ("CPF / CNPJ", lambda m: m.document or ""),
    ("RG", lambda m: m.id_card or ""),
    ("Nascimento", lambda m: m.birth_date or ""),
    ("Gênero", lambda m: m.gender or ""),
    ("Regional", lambda m: m.chapter or ""),
    ("E-mail", lambda m: m.email),
    ("E-mail secundário", lambda m: m.secondary_email or ""),
    ("Celular", lambda m: m.mobile or ""),
    ("Telefone secundário", lambda m: m.secondary_phone or ""),
    ("Empresa", lambda m: m.company or ""),
    ("Cargo / curso", lambda m: m.role or ""),
    ("LinkedIn", lambda m: m.linkedin or ""),
    ("Site", lambda m: m.website or ""),
    ("Inscrição estadual", lambda m: m.state_registration or ""),
    ("Inscrição municipal", lambda m: m.city_registration or ""),
    ("CEP", lambda m: m.postal_code or ""),
    ("Logradouro", lambda m: m.street or ""),
    ("Número", lambda m: m.number or ""),
    ("Complemento", lambda m: m.address_extra or ""),
    ("Bairro", lambda m: m.district or ""),
    ("Cidade", lambda m: m.city or ""),
    ("UF", lambda m: m.state or ""),
    ("Login", lambda m: m.login),
    ("Aceite dos termos", lambda m: _fmt_dt(m.accepted_terms_at)),
    ("IP do aceite", lambda m: m.accepted_terms_ip or ""),
    ("Protocolo", lambda m: m.id[:8].upper()),
)


MANDATE_LABELS = {
    "PENDING": "aguardando autorização",
    "ACTIVE": "débito autorizado",
    "REFUSED": "autorização recusada",
    "CANCELLED": "autorização cancelada",
    "EXPIRED": "autorização expirada",
}


def _method_label(member: Member) -> str:
    payment = max(member.payments, key=lambda p: p.created_at, default=None)
    if payment is None:
        return ""
    if payment.is_pix:
        state = MANDATE_LABELS.get(payment.mandate_status or "", "")
        return "Pix Automático" + (f" ({state})" if state else "")
    if payment.is_pix_invoice:
        return "Pix (cobrança a cada período)"
    return "Cartão de crédito"


def _fmt_dt(moment: datetime | None) -> str:
    value = local(moment)
    return value.strftime("%d/%m/%Y %H:%M") if value else ""


def _cell(value: str) -> str:
    # A cell starting with = + - @ is run as a formula by Excel. The data came
    # from a public form, so it is neutralised rather than trusted.
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def export_csv(db: Session, f: Filters) -> str:
    """The filtered list as a spreadsheet Excel opens correctly in pt-BR.

    Semicolon separator and a UTF-8 byte-order mark: with a comma, Brazilian
    Excel puts everything in column A; without the BOM, "Belém" arrives as
    "BelÃ©m".
    """
    members = db.scalars(
        apply_filters(select(Member), f).order_by(Member.created_at.desc())
    ).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow([name for name, _ in EXPORT_COLUMNS])
    for member in members:
        writer.writerow([_cell(str(getter(member))) for _, getter in EXPORT_COLUMNS])
    return "﻿" + buffer.getvalue()


# --------------------------------------------------------------------------
#  Audit trail
# --------------------------------------------------------------------------
ACTION_LABELS = {
    "entrou": "Entrou no painel",
    "saiu": "Saiu do painel",
    "login_falhou": "Tentativa de login recusada",
    "login_bloqueado": "Login bloqueado por excesso de tentativas",
    "trocou_senha": "Trocou a própria senha",
    "viu_ficha": "Abriu a ficha",
    "exportou": "Exportou planilha",
    "whatsapp": "Abriu conversa no WhatsApp",
    "conferiu_asaas": "Conferiu com a Asaas",
    "criou_usuario": "Criou usuário",
    "desativou_usuario": "Desativou usuário",
    "reativou_usuario": "Reativou usuário",
    "redefiniu_senha": "Redefiniu a senha de um usuário",
    "excluiu_associado": "Excluiu um cadastro",
    "trocou_pagamento": "Trocou a forma de pagamento",
    "adicionou_marca": "Adicionou logo de empresa associada",
    "removeu_marca": "Removeu logo de empresa associada",
    "ocultou_marca": "Ocultou logo de empresa associada",
    "mostrou_marca": "Voltou a mostrar logo de empresa associada",
    "reordenou_marcas": "Mudou a ordem das logos",
    "mudou_cor_marca": "Mudou a cor de uma logo (branca / original)",
}


def audit(
    db: Session,
    request,
    user: AdminUser | None,
    action: str,
    *,
    target: str | None = None,
    detail: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id if user else None,
            user_name=user.name if user else None,
            action=action,
            target=target[:255] if target else None,
            detail=detail,
            ip=client_ip(request),
        )
    )
    db.commit()


def recent_failed_logins(db: Session, email: str, minutes: int = 15) -> int:
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    return (
        db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "login_falhou",
                AuditLog.target == email,
                AuditLog.created_at >= since,
            )
        )
        or 0
    )


# --------------------------------------------------------------------------
#  Display formatting (stored as digits, shown the way people read them)
# --------------------------------------------------------------------------
def fmt_document(value: str | None) -> str:
    if not value:
        return "—"
    if len(value) == 11:
        return f"{value[:3]}.{value[3:6]}.{value[6:9]}-{value[9:]}"
    if len(value) == 14:
        return f"{value[:2]}.{value[2:5]}.{value[5:8]}/{value[8:12]}-{value[12:]}"
    return value


def fmt_phone(value: str | None) -> str:
    if not value:
        return "—"
    if len(value) == 11:
        return f"({value[:2]}) {value[2:7]}-{value[7:]}"
    if len(value) == 10:
        return f"({value[:2]}) {value[2:6]}-{value[6:]}"
    return value


def fmt_postal_code(value: str | None) -> str:
    if not value:
        return "—"
    return f"{value[:5]}-{value[5:]}" if len(value) == 8 else value


def fmt_iso_date(value: str | None) -> str:
    parsed = _parse_day(value or "")
    return parsed.strftime("%d/%m/%Y") if parsed else "—"


def fmt_moment(moment: datetime | None, pattern: str = "%d/%m/%Y %H:%M") -> str:
    value = local(moment)
    return value.strftime(pattern) if value else "—"


FORMATTERS = {
    "method_label": _method_label,
    "fmt_doc": fmt_document,
    "fmt_phone": fmt_phone,
    "fmt_cep": fmt_postal_code,
    "fmt_date": fmt_iso_date,
    "fmt_dt": fmt_moment,
    "brl": brl,
}

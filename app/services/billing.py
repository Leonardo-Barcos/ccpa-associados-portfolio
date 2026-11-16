"""Charges and reconciliation with the gateway.

Two jobs:

* `record_charge` stores one invoice as the gateway describes it. The webhook
  calls it for every event, so each monthly renewal is counted — the
  subscription row in `payments` alone would only ever show the first one.

* `reconcile` asks Asaas for every subscription and invoice and brings the
  local database in line. A webhook can be lost (the endpoint was down, the
  queue was paused after 15 failures); when that happens somebody paid and was
  never activated. Asaas is where the money is, so Asaas wins.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Charge, Member, MemberStatus, Payment, PaymentStatus
from ..plans import PLANS

log = logging.getLogger("ccpa.billing")

# Asaas statuses, grouped by what they mean for the association.
PAID = {"CONFIRMED", "RECEIVED", "RECEIVED_IN_CASH"}
REVERSED = {
    "REFUNDED",
    "REFUND_REQUESTED",
    "REFUND_IN_PROGRESS",
    "CHARGEBACK_REQUESTED",
    "CHARGEBACK_DISPUTE",
    "AWAITING_CHARGEBACK_REVERSAL",
}
OVERDUE = {"OVERDUE"}

CHARGE_LABELS = {
    "PENDING": "Aguardando",
    "CONFIRMED": "Pago (cartão aprovado)",
    "RECEIVED": "Pago (valor recebido)",
    "RECEIVED_IN_CASH": "Pago (em dinheiro)",
    "OVERDUE": "Vencida",
    "REFUNDED": "Estornada",
    "REFUND_REQUESTED": "Estorno solicitado",
    "REFUND_IN_PROGRESS": "Estorno em andamento",
    "CHARGEBACK_REQUESTED": "Contestada (chargeback)",
    "CHARGEBACK_DISPUTE": "Contestação em disputa",
    "AWAITING_CHARGEBACK_REVERSAL": "Aguardando reversão de contestação",
    "DELETED": "Removida",
}

# A date without a time is pinned to midday in Belém, so it never slides into
# the neighbouring day — or month — when it is converted to UTC and back.
_BELEM = timezone(timedelta(hours=-3))


def _as_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        day = datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return datetime.combine(day, time(12, 0), tzinfo=_BELEM).astimezone(UTC)


def is_paid(status: str) -> bool:
    return status in PAID


def record_charge(
    db: Session, member: Member, invoice: dict, *, event: str | None = None
) -> Charge | None:
    """Create or update the local copy of one gateway invoice.

    Returns None when the payload does not describe an invoice.
    """
    provider_id = invoice.get("id")
    if not provider_id:
        return None

    status = invoice.get("status") or "PENDING"
    if event == "PAYMENT_DELETED" or invoice.get("deleted"):
        status = "DELETED"

    charge = db.scalar(select(Charge).where(Charge.provider_id == provider_id))
    if charge is None:
        charge = Charge(member_id=member.id, provider_id=provider_id)
        db.add(charge)

    value = invoice.get("value")
    if value is not None:
        charge.amount_cents = round(float(value) * 100)
    elif charge.amount_cents is None:
        plan = PLANS.get(member.plan_code)
        charge.amount_cents = plan.amount_cents if plan else 0

    charge.status = status
    charge.due_date = (invoice.get("dueDate") or charge.due_date or "")[:10] or None
    charge.invoice_url = invoice.get("invoiceUrl") or charge.invoice_url

    if status in PAID:
        charge.paid_at = (
            _as_datetime(
                invoice.get("confirmedDate")
                or invoice.get("paymentDate")
                or invoice.get("clientPaymentDate")
            )
            or charge.paid_at
            or datetime.now(UTC)
        )
    elif status in REVERSED or status == "DELETED":
        # A reversed charge is money the association no longer has. Keeping
        # paid_at would leave it in the revenue chart.
        charge.paid_at = None

    return charge


# --------------------------------------------------------------------------
#  Deriving a member's status from their invoices
# --------------------------------------------------------------------------
def derive_status(
    charges: list[Charge], *, subscription_deleted: bool = False
) -> MemberStatus:
    """What the member's status should be, judging by the gateway alone.

    * the subscription was removed in Asaas            -> CANCELLED
    * the latest settled invoice was reversed          -> CANCELLED
    * an invoice is overdue                            -> OVERDUE
    * at least one invoice was paid                    -> ACTIVE
    * nothing paid yet                                 -> AWAITING_PAYMENT
    """
    if subscription_deleted:
        return MemberStatus.CANCELLED

    live = [c for c in charges if c.status != "DELETED"]
    settled = sorted(
        (c for c in live if c.status in PAID | REVERSED),
        key=lambda c: c.due_date or "",
    )
    if settled and settled[-1].status in REVERSED:
        return MemberStatus.CANCELLED
    if any(c.status in OVERDUE for c in live):
        return MemberStatus.OVERDUE
    if any(c.status in PAID for c in live):
        return MemberStatus.ACTIVE
    return MemberStatus.AWAITING_PAYMENT


# --------------------------------------------------------------------------
#  Reconciliation
# --------------------------------------------------------------------------
@dataclass
class StatusChange:
    member: Member
    before: MemberStatus
    after: MemberStatus


@dataclass
class ReconcileReport:
    checked: int = 0
    charges_created: int = 0
    charges_updated: int = 0
    changes: list[StatusChange] = field(default_factory=list)
    not_found: list[Member] = field(default_factory=list)
    error: str | None = None


def _asaas_client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.asaas_base_url,
        headers={"access_token": settings.asaas_api_key},
        timeout=20.0,
    )


def _fetch_all(client: httpx.Client, path: str, **params) -> list[dict]:
    """Walk Asaas's offset pagination (100 per page, the maximum)."""
    items: list[dict] = []
    offset = 0
    while True:
        response = client.get(path, params={**params, "limit": 100, "offset": offset})
        response.raise_for_status()
        body = response.json()
        page = body.get("data", [])
        items.extend(page)
        if not body.get("hasMore") or not page:
            return items
        offset += len(page)


def reconcile(db: Session, client: httpx.Client | None = None) -> ReconcileReport:
    """Bring every Asaas-billed member in line with what Asaas reports.

    Two list calls, whatever the number of members, rather than one call per
    member — the function has a 30-second budget on Vercel.
    """
    report = ReconcileReport()

    if settings.payment_provider != "asaas" and client is None:
        report.error = (
            "A conferência só funciona com PAYMENT_PROVIDER=asaas. "
            "Em desenvolvimento os pagamentos são simulados."
        )
        return report
    if not settings.asaas_api_key and client is None:
        report.error = "ASAAS_API_KEY não está configurada."
        return report

    subscriptions_by_member: dict[str, Payment] = {}
    for payment in db.scalars(
        select(Payment)
        .where(Payment.provider == "asaas", Payment.external_reference.is_not(None))
        .order_by(Payment.created_at)
    ):
        # The newest subscription wins if a member ever had more than one.
        subscriptions_by_member[payment.member_id] = payment

    if not subscriptions_by_member:
        return report

    owns_client = client is None
    client = client or _asaas_client()
    try:
        remote_subscriptions = {
            s["id"]: s
            for s in _fetch_all(client, "/subscriptions", includeDeleted="true")
        }
        invoices_by_subscription: dict[str, list[dict]] = {}
        invoices_by_customer: dict[str, list[dict]] = {}
        for invoice in _fetch_all(client, "/payments"):
            if invoice.get("subscription"):
                invoices_by_subscription.setdefault(invoice["subscription"], []).append(
                    invoice
                )
            if invoice.get("customer"):
                invoices_by_customer.setdefault(invoice["customer"], []).append(invoice)

        # Pix Automático: the authorization is the "subscription". One call
        # per Pix member — there is no list endpoint filtered by our ids, and
        # the number of Pix members is what it is.
        authorizations: dict[str, dict] = {}
        for payment in subscriptions_by_member.values():
            if not payment.is_pix:
                continue
            found = client.get(
                f"/pix/automatic/authorizations/{payment.external_reference}"
            )
            if found.status_code == 200:
                authorizations[payment.external_reference] = found.json()
    except httpx.HTTPError as error:
        log.exception("Reconciliation failed talking to Asaas")
        report.error = f"Não foi possível falar com a Asaas: {error}"
        return report
    finally:
        if owns_client:
            client.close()

    known = {c.provider_id for c in db.scalars(select(Charge))}

    for member_id, payment in subscriptions_by_member.items():
        member = db.get(Member, member_id)
        if member is None:
            continue
        report.checked += 1

        if payment.is_pix:
            authorization = authorizations.get(payment.external_reference)
            invoices = invoices_by_customer.get(payment.provider_customer_id or "", [])
            if authorization is None and not invoices:
                report.not_found.append(member)
                continue
            mandate = (authorization or {}).get("status")
            if mandate:
                payment.mandate_status = (
                    "PENDING" if mandate.startswith("PENDING") else mandate
                )
            ended = payment.mandate_status in ("CANCELLED", "EXPIRED")
            # An authorization that ended before anything was paid is a QR
            # code nobody used, not a membership that was cancelled.
            ever_paid = any(i.get("status") in PAID | REVERSED for i in invoices)
            subscription = {"deleted": ended and ever_paid}
        else:
            subscription = remote_subscriptions.get(payment.external_reference)
            invoices = invoices_by_subscription.get(payment.external_reference, [])
            if subscription is None and not invoices:
                report.not_found.append(member)
                continue

        for invoice in invoices:
            if invoice["id"] in known:
                report.charges_updated += 1
            else:
                report.charges_created += 1
                known.add(invoice["id"])
            record_charge(db, member, invoice)
        db.flush()

        charges = list(db.scalars(select(Charge).where(Charge.member_id == member.id)))
        new_status = derive_status(
            charges,
            subscription_deleted=bool(subscription and subscription.get("deleted")),
        )

        # The subscription row mirrors the most recent invoice.
        paid = [c for c in charges if c.status in PAID and c.paid_at]
        if paid:
            payment.status = PaymentStatus.CONFIRMED
            payment.paid_at = payment.paid_at or min(c.paid_at for c in paid)
        if new_status == MemberStatus.CANCELLED and any(
            c.status in REVERSED for c in charges
        ):
            payment.status = PaymentStatus.REFUNDED

        if member.status != new_status:
            report.changes.append(StatusChange(member, member.status, new_status))
            member.status = new_status

    db.commit()
    return report

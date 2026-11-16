"""Payment confirmation callback from the gateway."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Member, MemberStatus, Payment, PaymentStatus
from ..plans import PLANS
from ..services.billing import record_charge
from ..services.email import send_payment_confirmed
from ..services.payments import get_payment_provider

log = logging.getLogger("ccpa.webhook")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_TRANSITIONS = {
    "CONFIRMED": (PaymentStatus.CONFIRMED, MemberStatus.ACTIVE),
    "FAILED": (PaymentStatus.FAILED, MemberStatus.OVERDUE),
    "REFUNDED": (PaymentStatus.REFUNDED, MemberStatus.CANCELLED),
}


@router.post("/pagamento")
async def payment_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    provider = get_payment_provider()

    if not provider.verify_webhook(dict(request.headers), body):
        log.warning("Webhook rejected: bad signature or token.")
        raise HTTPException(status_code=401, detail="Invalid signature.")

    payload = await request.json()

    if payload.get("authorization"):
        return _authorization_event(db, payload)

    reference, status = provider.parse_event(payload)
    invoice = payload.get("payment") or {}

    payment = None
    if reference:
        payment = (
            db.query(Payment)
            .filter(Payment.external_reference == reference)
            .order_by(Payment.created_at.desc())
            .first()
        )
    if payment is None and invoice.get("customer"):
        # Pix Automático charges come from a subscription Asaas creates on its
        # own; its id was never ours to store. The customer id was.
        payment = (
            db.query(Payment)
            .filter(Payment.provider_customer_id == invoice["customer"])
            .order_by(Payment.created_at.desc())
            .first()
        )
    if not payment:
        log.warning("Webhook for unknown reference: %s", reference)
        return {"ok": True, "ignored": True}

    # Every invoice is recorded, before any early return below: the second,
    # third and twelfth monthly charge leave the subscription's status exactly
    # where it was, and without this they would never be counted as revenue.
    member = db.get(Member, payment.member_id)
    if member and invoice.get("id"):
        record_charge(db, member, invoice, event=payload.get("event"))
        db.commit()

    if not status:
        return {"ok": True, "ignored": True}

    new_payment_status, new_member_status = _TRANSITIONS[status]

    # Idempotency. Gateways retry, and a replayed event must not send the
    # welcome e-mail twice.
    if payment.status == new_payment_status:
        return {"ok": True, "duplicate": True}

    payment.status = new_payment_status
    if new_payment_status == PaymentStatus.CONFIRMED:
        payment.paid_at = datetime.now(UTC)

    if member:
        member.status = new_member_status
    db.commit()

    if member and new_payment_status == PaymentStatus.CONFIRMED:
        send_payment_confirmed(db, member, PLANS[member.plan_code])

    return {"ok": True}


# Pix Automático: what the end of an authorization means for the member.
_MANDATE_ENDED = {"CANCELLED", "EXPIRED"}


def _authorization_event(db: Session, payload: dict) -> dict:
    """React to the bank's answer about a Pix Automático authorization.

    * ACTIVE — the recurring debit is authorised; nothing else changes, the
      member was activated by the first payment.
    * REFUSED — the bank did not confirm (or the QR code expired unpaid).
      Before any payment this only marks the QR code as dead, so the success
      page offers a new one; after it, the member keeps the period they paid.
    * CANCELLED / EXPIRED — no more debits. A member who was active is
      cancelled, as a card member is when the subscription ends.
    """
    from ..services.payments import AsaasProvider

    event = payload.get("event", "")
    authorization = payload["authorization"]
    new_state = AsaasProvider.AUTHORIZATION_EVENTS.get(event) or authorization.get(
        "status"
    )
    payment = (
        db.query(Payment)
        .filter(Payment.external_reference == authorization.get("id"))
        .order_by(Payment.created_at.desc())
        .first()
    )
    if payment is None or not new_state:
        log.warning("Authorization event for unknown id: %s", authorization.get("id"))
        return {"ok": True, "ignored": True}
    if payment.mandate_status == new_state:
        return {"ok": True, "duplicate": True}

    payment.mandate_status = new_state
    member = db.get(Member, payment.member_id)
    paid = payment.status == PaymentStatus.CONFIRMED

    if new_state == "REFUSED" and not paid:
        payment.status = PaymentStatus.FAILED
    if (
        new_state in _MANDATE_ENDED
        and member
        and member.status in (MemberStatus.ACTIVE, MemberStatus.OVERDUE)
    ):
        member.status = MemberStatus.CANCELLED
    if new_state in _MANDATE_ENDED and not paid:
        payment.status = PaymentStatus.FAILED
    db.commit()
    log.info("Pix authorization %s -> %s", authorization.get("id"), new_state)
    return {"ok": True}

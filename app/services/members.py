"""Business rules for joining the association."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import EntityType, Member, MemberStatus, Payment, PaymentStatus
from ..plans import PLANS, Plan
from ..schemas import CompanySignup, IndividualSignup
from ..security import hash_password
from .email import send_welcome_with_payment_link
from .payments import get_payment_provider


class BusinessError(Exception):
    """An expected failure shown back on the form, next to the offending field."""

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(message)


def _reject_duplicates(
    db: Session,
    *,
    email: str,
    login: str,
    document: str | None,
    document_field: str = "cpf",
) -> None:
    if db.scalar(
        select(func.count())
        .select_from(Member)
        .where(func.lower(Member.login) == login.lower())
    ):
        raise BusinessError("login", "Este login já está em uso.")

    if db.scalar(
        select(func.count())
        .select_from(Member)
        .where(func.lower(Member.email) == email.lower())
    ):
        raise BusinessError(
            "email", "Já existe uma associação com este e-mail. Fale conosco."
        )

    if document and db.scalar(
        select(func.count()).select_from(Member).where(Member.document == document)
    ):
        # The field is named after the form input ("cpf" or "cnpj"): an error
        # keyed to anything else has nowhere to be shown, and the visitor is
        # left with "check the highlighted fields" and nothing highlighted.
        label = "CPF" if document_field == "cpf" else "CNPJ"
        raise BusinessError(
            document_field,
            f"Este {label} já tem um cadastro no clube. Se você ainda não "
            "concluiu o pagamento, fale com a gente em criativosdopara@gmail.com "
            "que enviamos o seu link.",
        )


def _shared_fields(data) -> dict:
    return {
        "mobile": data.mobile,
        "secondary_phone": data.secondary_phone,
        "email": str(data.email),
        "secondary_email": str(data.secondary_email) if data.secondary_email else None,
        "postal_code": data.postal_code,
        "street": data.street,
        "number": data.number,
        "district": data.district,
        "state": data.state,
        "city": data.city,
        "address_extra": data.address_extra,
        "login": data.login,
        "password_hash": hash_password(data.password),
        "accepted_terms": data.accepted_terms,
    }


def build_member(data: IndividualSignup | CompanySignup) -> Member:
    shared = _shared_fields(data)

    if isinstance(data, IndividualSignup):
        return Member(
            entity_type=EntityType.PF,
            plan_code=data.plan_code,
            name=data.name,
            document=data.cpf,
            id_card=data.id_card,
            birth_date=data.birth_date.isoformat() if data.birth_date else None,
            gender=data.gender,
            linkedin=data.linkedin,
            company=data.company,
            role=data.role,
            chapter=data.chapter,
            **shared,
        )

    return Member(
        entity_type=EntityType.PJ,
        plan_code=data.plan_code,
        name=data.legal_name,
        trade_name=data.trade_name,
        document=data.cnpj,
        state_registration=data.state_registration,
        city_registration=data.city_registration,
        chapter=data.chapter,
        website=data.website,
        **shared,
    )


def register_signup(
    db: Session,
    data: IndividualSignup | CompanySignup,
    *,
    ip: str | None = None,
) -> tuple[Member, Payment]:
    """Create the member, open the charge and send the payment link."""
    document = getattr(data, "cpf", None) or getattr(data, "cnpj", None)
    _reject_duplicates(
        db,
        email=str(data.email),
        login=data.login,
        document=document,
        document_field="cpf" if isinstance(data, IndividualSignup) else "cnpj",
    )

    member = build_member(data)
    member.accepted_terms_ip = ip
    member.accepted_terms_at = datetime.now(UTC)
    member.status = MemberStatus.AWAITING_PAYMENT

    db.add(member)
    db.flush()  # assigns the id without committing yet

    plan: Plan = PLANS[member.plan_code]
    link = get_payment_provider().create_charge(member, plan, data.payment_method)

    payment = Payment(
        member_id=member.id,
        provider=link.provider,
        external_reference=link.external_reference,
        amount_cents=plan.amount_cents,
        cycle=plan.cycle,
        status=PaymentStatus.PENDING,
        method=link.method,
    )
    apply_checkout(payment, link, member)
    db.add(payment)
    db.commit()
    db.refresh(member)
    db.refresh(payment)

    send_welcome_with_payment_link(
        db, member, plan, payment.checkout_url, pix=payment.is_pix
    )
    return member, payment


def apply_checkout(payment: Payment, link, member: Member) -> None:
    """Copy what the gateway returned onto the payment row.

    For Pix Automático the "checkout" is our own success page, which shows
    the QR code — so the link in the e-mail, the WhatsApp message and the
    panel's copy button all lead somewhere the member can actually pay.
    """
    from ..config import settings

    payment.provider_customer_id = link.customer_id or payment.provider_customer_id
    payment.external_reference = link.external_reference
    payment.method = link.method
    if link.method == "PIX_AUTOMATIC":
        payment.checkout_url = (
            f"{settings.base_url.rstrip('/')}/associe-se/sucesso/{member.id}"
        )
        payment.mandate_status = link.mandate_status
        payment.pix_qr_image = link.pix_qr_image
        payment.pix_qr_payload = link.pix_qr_payload
        payment.pix_qr_expires_at = link.pix_qr_expires_at
    else:
        # No open invoice right now (a card switched to Pix mid-period): the
        # link already on file still leads to the member's invoice page.
        payment.checkout_url = link.url or payment.checkout_url
        payment.mandate_status = None
        payment.pix_qr_image = None
        payment.pix_qr_payload = None
        payment.pix_qr_expires_at = None

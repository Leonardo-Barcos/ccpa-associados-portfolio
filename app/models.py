"""Database models."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


# (str, Enum) rather than StrEnum is deliberate: SQLAlchemy stores the member
# value as text, which keeps the database readable and portable.
class EntityType(str, enum.Enum):
    PF = "PF"  # individual
    PJ = "PJ"  # company


class MemberStatus(str, enum.Enum):
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    ACTIVE = "ACTIVE"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("document", name="uq_member_document"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entity_type: Mapped[EntityType] = mapped_column(Enum(EntityType), index=True)
    plan_code: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[MemberStatus] = mapped_column(
        Enum(MemberStatus), default=MemberStatus.AWAITING_PAYMENT, index=True
    )

    # Identity. For individuals: full name and CPF. For companies: legal name
    # and CNPJ. One column pair serves both so the member list stays a single
    # table instead of two that have to be unioned on every query.
    name: Mapped[str] = mapped_column(String(180))
    trade_name: Mapped[str | None] = mapped_column(String(180), nullable=True)
    document: Mapped[str | None] = mapped_column(String(14), nullable=True, index=True)
    id_card: Mapped[str | None] = mapped_column(String(20), nullable=True)
    birth_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(40), nullable=True)
    state_registration: Mapped[str | None] = mapped_column(String(30), nullable=True)
    city_registration: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Professional profile
    linkedin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(180), nullable=True)
    role: Mapped[str | None] = mapped_column(String(120), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    chapter: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    # Contact
    mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    secondary_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    secondary_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Address
    postal_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    district: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    address_extra: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Member area credentials
    login: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))

    # Consent trail. Brazil's data protection law (LGPD) expects the
    # organisation to be able to prove when and from where consent was given.
    accepted_terms: Mapped[bool] = mapped_column(Boolean, default=False)
    accepted_terms_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    accepted_terms_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    payments: Mapped[list[Payment]] = relationship(
        back_populates="member", cascade="all, delete-orphan"
    )
    charges: Mapped[list[Charge]] = relationship(
        back_populates="member",
        cascade="all, delete-orphan",
        order_by="Charge.due_date.desc()",
    )

    @property
    def first_name(self) -> str:
        return (self.trade_name or self.name).split(" ")[0]


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    member_id: Mapped[str] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(30))
    external_reference: Mapped[str | None] = mapped_column(
        String(120), nullable=True, index=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer)
    cycle: Mapped[str] = mapped_column(String(10), default="MONTHLY")
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.PENDING, index=True
    )
    checkout_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # ---- added with Pix Automático ------------------------------------
    # Every column below is nullable or has a server default, so
    # `ensure_schema` can add it to a database that already has members.
    #
    # How the member pays: "CREDIT_CARD", "PIX" (an Asaas subscription billed
    # as Pix: each period a new charge with its own QR code, paid by hand) or
    # "PIX_AUTOMATIC" (recurring debit authorised in the bank app).
    method: Mapped[str] = mapped_column(
        String(20), default="CREDIT_CARD", server_default="CREDIT_CARD"
    )
    # The gateway's customer id. Every invoice Asaas sends carries it, which
    # is what ties a Pix Automático charge back to its member: those charges
    # come from a subscription Asaas creates on its own, whose id we never
    # see at signup.
    provider_customer_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Pix Automático only: the authorization's state as the bank reports it
    # (PENDING, ACTIVE, REFUSED, CANCELLED, EXPIRED).
    mandate_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # The first-payment QR code, shown on our own page — Pix Automático has
    # no hosted checkout to redirect to.
    pix_qr_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    pix_qr_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    pix_qr_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    member: Mapped[Member] = relationship(back_populates="payments")

    @property
    def is_pix(self) -> bool:
        """Pix Automático: the QR code lives on our page, not in Asaas's."""
        return self.method == "PIX_AUTOMATIC"

    @property
    def is_pix_invoice(self) -> bool:
        """Plain Pix: Asaas's invoice page shows the QR code each period."""
        return self.method == "PIX"


class EmailLog(Base):
    """Audit trail for outgoing e-mail.

    Kept because a signup that never received its payment link is otherwise
    invisible: the member simply does not come back.
    """

    __tablename__ = "email_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recipient: Mapped[str] = mapped_column(String(255))
    template: Mapped[str] = mapped_column(String(60))
    provider_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ==========================================================================
#  Cobranças individuais
# ==========================================================================
class Charge(Base):
    """One invoice of a subscription, as the gateway reports it.

    `Payment` is the subscription the signup opened, and it holds a single
    row per member. A monthly member is charged twelve times a year, though,
    and every one of those charges has to be counted for the revenue figures
    to mean anything. Each charge is stored here, keyed by the gateway's own
    id, so a replayed webhook or a second reconciliation updates the row
    instead of counting the money twice.

    `status` keeps the gateway's word verbatim (PENDING, CONFIRMED, RECEIVED,
    OVERDUE, REFUNDED, ...): translating it here would lose the difference
    between a card that was approved and money that already settled.
    """

    __tablename__ = "charges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    member_id: Mapped[str] = mapped_column(
        ForeignKey("members.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40), index=True)
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    member: Mapped[Member] = relationship(back_populates="charges")


# ==========================================================================
#  Painel de gestão
# ==========================================================================
class AdminRole(str, enum.Enum):
    ADMIN = "ADMIN"  # everything, including exports and user management
    EQUIPE = "EQUIPE"  # sees and follows up on members; exports nothing


class AdminUser(Base):
    """Someone who signs in to the management panel.

    Deliberately a separate table from `members`: a member's login opens
    nothing here, and a panel account is not a membership.
    """

    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    role: Mapped[AdminRole] = mapped_column(Enum(AdminRole), default=AdminRole.EQUIPE)
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Set whenever someone else chose the password (a new account, a reset):
    # the first sign-in then goes straight to "choose your own".
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    # Bumped on every password change. It is part of the session cookie, so
    # changing the password signs out every other open session.
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_admin(self) -> bool:
        return self.role == AdminRole.ADMIN


class AuditLog(Base):
    """Who looked at or changed what, and when.

    The panel shows CPF, RG, birth date and home address. Under the LGPD the
    association has to be able to answer who accessed a member's data; this
    table is that answer. The user's name is copied in, so the trail still
    reads correctly after an account is deactivated.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    user_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


# ==========================================================================
#  Empresas associadas (faixa de logos na página de cadastro)
# ==========================================================================
class BrandLogo(Base):
    """A member company's logo, shown on the signup page.

    The image lives in the database rather than on disk: the serverless
    filesystem is thrown away with each instance, so an uploaded file would
    vanish on the next deploy. Logos are small (the upload is capped), and
    they are served with a year-long cache keyed to `updated_at`, so the
    database is read once per logo per visitor, not once per page view.
    """

    __tablename__ = "brand_logos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120))
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    content_type: Mapped[str] = mapped_column(String(20))
    position: Mapped[int] = mapped_column(Integer, default=0, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # The member this logo belongs to, when it is one (it usually is). Plain
    # column, no foreign key: deleting the member only unlinks the logo, and
    # `ensure_schema` can add the column to a table that already has rows.
    member_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # How it appears on the dark hero: "branca" (redrawn in white, straight
    # on the background — the default, so every logo reads the same) or
    # "original" (its own colours, on a white card).
    tone: Mapped[str] = mapped_column(
        String(10), default="branca", server_default="branca"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    @property
    def version(self) -> str:
        """Cache-busting token for the image URL."""
        moment = self.updated_at or self.created_at
        return str(int(moment.timestamp())) if moment else "0"

    @property
    def url(self) -> str:
        return f"/marcas/{self.id}?v={self.version}"

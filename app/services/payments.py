"""Payment layer.

The application never talks to a gateway directly: it asks the configured
provider for a `CheckoutLink`. Swapping gateways means writing one class here
and changing one environment variable. Nothing else in the system moves.

Providers:
  * MockProvider  — development, an internal fake checkout.
  * AsaasProvider — recurring credit-card subscription through Asaas.

Credit card only. The card is never typed into this application: Asaas hosts
the checkout page and we redirect to it. That keeps card data out of our
servers, our logs and our database, which is what keeps the project outside
PCI-DSS scope.
"""

from __future__ import annotations

import hmac
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone

import httpx

from ..config import settings
from ..models import Member
from ..plans import Plan

log = logging.getLogger("ccpa.payments")

_BRT = timezone(timedelta(hours=-3))  # Asaas reports local (Brasília) times

# Asaas dates the first invoice; the member needs a little room to pay it
# without the subscription going overdue on the same day it was created.
FIRST_DUE_DATE_DAYS = 3


class PaymentError(Exception):
    """The gateway refused to open the charge.

    `message` is safe to show the visitor; `detail` is what goes to the log —
    the gateway's own words, which is what makes the failure fixable.
    """

    def __init__(self, message: str, detail: str = ""):
        self.message = message
        self.detail = detail
        super().__init__(detail or message)


GENERIC_PAYMENT_ERROR = (
    "Não conseguimos gerar a sua cobrança agora. Seus dados não foram salvos; "
    "tente de novo em alguns minutos. Se continuar, fale com a gente em "
    "criativosdopara@gmail.com."
)


def _asaas_error(response: httpx.Response, step: str) -> PaymentError:
    """Turn an Asaas refusal into a message a visitor can act on.

    Asaas answers validation problems with 400 and a list of
    {"code", "description"} in Portuguese ("O CPF/CNPJ informado é
    inválido."). Those descriptions are about the visitor's own data, so they
    are shown. Anything else (401: wrong key, 5xx: Asaas down) is our problem,
    and the visitor gets the generic message.
    """
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        errors = []
    descriptions = [e.get("description", "") for e in errors if e.get("description")]
    detail = f"Asaas {step} -> HTTP {response.status_code}: " + (
        "; ".join(descriptions) or response.text[:500]
    )
    if response.status_code == 400 and descriptions:
        return PaymentError(
            "A Asaas, que processa o pagamento, recusou o cadastro: "
            + " ".join(descriptions)
            + " Confira os dados e envie novamente.",
            detail,
        )
    return PaymentError(GENERIC_PAYMENT_ERROR, detail)


CARD = "CREDIT_CARD"
# A regular Asaas subscription billed as Pix: every period Asaas issues a new
# charge whose invoice page shows the QR code, and the member pays it by
# hand. Needs no special approval from Asaas, unlike Pix Automático. Not
# offered at signup — it is what the team switches a member to in the panel
# when they would rather pay by Pix.
PIX = "PIX"
PIX_AUTOMATIC = "PIX_AUTOMATIC"
METHODS = (CARD, PIX_AUTOMATIC)
# The ones a Subscription in Asaas can be switched between in place.
SUBSCRIPTION_METHODS = (CARD, PIX)

METHOD_LABELS = {
    CARD: "Cartão de crédito",
    PIX: "Pix (cobrança a cada período)",
    PIX_AUTOMATIC: "Pix Automático",
}

# How long the first-payment QR code stays valid. Long enough to pay later the
# same day; when it expires the success page offers a fresh one.
PIX_QR_SECONDS = 60 * 60 * 24


def _add_months(day: date, months: int) -> date:
    """Same day, `months` later — or the month's last day when it is shorter
    (31 January + 1 month = 28/29 February)."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    for candidate in (day.day, 30, 29, 28):
        try:
            return date(year, month, min(day.day, candidate))
        except ValueError:
            continue
    raise ValueError(day)


@dataclass
class CheckoutLink:
    """What the gateway handed back when the charge was opened.

    For the card, `url` is Asaas's hosted checkout. For Pix Automático there
    is no hosted page: `url` is None and the QR code travels in `pix_qr_*`,
    to be shown on our own success page.
    """

    url: str | None
    external_reference: str | None
    provider: str
    method: str = CARD
    customer_id: str | None = None
    mandate_status: str | None = None
    pix_qr_image: str | None = None
    pix_qr_payload: str | None = None
    pix_qr_expires_at: datetime | None = None


class PaymentProvider(ABC):
    name: str = "base"

    @abstractmethod
    def create_charge(
        self, member: Member, plan: Plan, method: str = CARD
    ) -> CheckoutLink: ...

    def renew_pix(self, member: Member, plan: Plan, payment) -> CheckoutLink:
        """A fresh first-payment QR code, replacing one that expired."""
        return self.create_charge(member, plan, PIX_AUTOMATIC)

    def change_method(
        self, member: Member, plan: Plan, payment, new_method: str
    ) -> CheckoutLink:
        """Switch how an existing member pays, keeping the registration.

        Between card and Pix the gateway's subscription is kept and only its
        billing type changes, so its history and due dates stay as they are.
        Pix Automático is a different object (an authorization, not a
        subscription), so moving in or out of it opens the new one and
        closes the old.
        """
        raise NotImplementedError

    def verify_webhook(self, headers: dict, body: bytes) -> bool:  # noqa: ARG002
        return True

    def parse_event(self, payload: dict) -> tuple[str | None, str | None]:
        """Return (external_reference, normalised status)."""
        return None, None


class MockProvider(PaymentProvider):
    """Fake checkout, so the whole flow can be demonstrated before the Asaas
    account exists — and so the test suite never touches the network."""

    name = "mock"

    def create_charge(
        self,
        member: Member,
        plan: Plan,  # noqa: ARG002
        method: str = CARD,
    ) -> CheckoutLink:
        if method == PIX_AUTOMATIC:
            return CheckoutLink(
                url=None,
                external_reference=f"mock_pix_{member.id[:8]}",
                provider=self.name,
                method=PIX_AUTOMATIC,
                customer_id=f"mock_cus_{member.id[:8]}",
                mandate_status="PENDING",
                # A real QR is not needed to try the flow: the page shows the
                # copy-and-paste code and the simulated checkout pays it.
                pix_qr_payload=f"00020101SIMULADO-CCPA-{member.id[:8].upper()}",
                pix_qr_expires_at=datetime.now(UTC) + timedelta(seconds=PIX_QR_SECONDS),
            )
        return CheckoutLink(
            url=f"{settings.base_url}/pagamento/simulado/{member.id}",
            external_reference=f"mock_{member.id[:8]}",
            provider=self.name,
            method=method,
            customer_id=f"mock_cus_{member.id[:8]}",
        )

    def change_method(
        self, member: Member, plan: Plan, payment, new_method: str
    ) -> CheckoutLink:
        if new_method in SUBSCRIPTION_METHODS and payment.method in SUBSCRIPTION_METHODS:
            return CheckoutLink(
                url=f"{settings.base_url}/pagamento/simulado/{member.id}",
                external_reference=payment.external_reference,
                provider=self.name,
                method=new_method,
                customer_id=payment.provider_customer_id,
            )
        return self.create_charge(member, plan, new_method)


class AsaasProvider(PaymentProvider):
    """Recurring credit-card subscription through Asaas.

    Two calls open the charge: a customer, then a subscription billed as
    `CREDIT_CARD`. Asaas answers with the hosted invoice page — the only place
    the card number is ever typed.
    """

    name = "asaas"

    # Asaas event names, mapped to the four states this application knows.
    # CHARGEBACK is card-specific and has to end the membership: the money has
    # already been taken back from the association.
    #
    # Public because `scripts/setup_webhook.py` subscribes the webhook to
    # exactly these keys: an event nobody handles is noise, and one that is
    # handled but never sent is a membership that silently never activates.
    STATUS_MAP = {
        "PAYMENT_CONFIRMED": "CONFIRMED",
        "PAYMENT_RECEIVED": "CONFIRMED",
        "PAYMENT_CREDIT_CARD_CAPTURE_REFUSED": "FAILED",
        "PAYMENT_OVERDUE": "FAILED",
        "PAYMENT_DELETED": "FAILED",
        "PAYMENT_REFUNDED": "REFUNDED",
        "PAYMENT_CHARGEBACK_REQUESTED": "REFUNDED",
        "PAYMENT_CHARGEBACK_DISPUTE": "REFUNDED",
    }

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=settings.asaas_base_url,
            headers={
                "access_token": settings.asaas_api_key,
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )

    # Pix Automático authorization events, mapped to the authorization's
    # state. Public for the same reason as STATUS_MAP.
    AUTHORIZATION_EVENTS = {
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CREATED": "PENDING",
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED": "ACTIVE",
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_REFUSED": "REFUSED",
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CANCELLED": "CANCELLED",
        "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_EXPIRED": "EXPIRED",
    }

    def create_charge(
        self, member: Member, plan: Plan, method: str = CARD
    ) -> CheckoutLink:
        if not settings.asaas_api_key:
            raise PaymentError(GENERIC_PAYMENT_ERROR, "ASAAS_API_KEY is not configured")

        try:
            with self._client() as client:
                customer_id = self._create_customer(client, member)
                if method == PIX_AUTOMATIC:
                    return self._open_pix_automatic(client, member, plan, customer_id)
                return self._open_subscription(client, member, plan, customer_id)
        except PaymentError:
            raise
        except httpx.HTTPError as error:
            # Timeout, DNS, connection reset: Asaas never answered.
            raise PaymentError(
                GENERIC_PAYMENT_ERROR, f"Asaas unreachable: {error!r}"
            ) from error

    def renew_pix(self, member: Member, plan: Plan, payment) -> CheckoutLink:
        """Replace an expired or refused first-payment QR code.

        The old authorization is cancelled first (a stale one could still be
        paid), and the customer already registered in Asaas is reused.
        """
        if not settings.asaas_api_key:
            raise PaymentError(GENERIC_PAYMENT_ERROR, "ASAAS_API_KEY is not configured")
        try:
            with self._client() as client:
                if payment.external_reference:
                    old = client.delete(
                        f"/pix/automatic/authorizations/{payment.external_reference}"
                    )
                    if old.is_error and old.status_code not in (400, 404):
                        raise _asaas_error(old, "DELETE /pix/automatic/authorizations")
                customer_id = payment.provider_customer_id or self._create_customer(
                    client, member
                )
                return self._open_pix_automatic(client, member, plan, customer_id)
        except PaymentError:
            raise
        except httpx.HTTPError as error:
            raise PaymentError(
                GENERIC_PAYMENT_ERROR, f"Asaas unreachable: {error!r}"
            ) from error

    def _create_customer(self, client: httpx.Client, member: Member) -> str:
        customer = client.post(
            "/customers",
            json={
                "name": member.name,
                "email": member.email,
                "cpfCnpj": member.document,
                "mobilePhone": member.mobile,
                "postalCode": member.postal_code,
                "externalReference": member.id,
            },
        )
        if customer.is_error:
            raise _asaas_error(customer, "POST /customers")
        return customer.json()["id"]

    def _open_pix_automatic(
        self, client: httpx.Client, member: Member, plan: Plan, customer_id: str
    ) -> CheckoutLink:
        """Open a Pix Automático authorization with its first payment.

        One QR code does two things: it pays the first period now, and paying
        it asks the payer's bank to authorise the recurring debit. From then
        on Asaas generates each charge itself (paymentCreationMode
        SUBSCRIPTION), so the recurring authorization starts one period
        ahead — starting today would charge the first period twice.
        """
        today = date.today()
        yearly = plan.cycle == "YEARLY"
        response = client.post(
            "/pix/automatic/authorizations",
            json={
                "customerId": customer_id,
                "frequency": "ANNUALLY" if yearly else "MONTHLY",
                # Max 35 characters: the member id without its hyphens (32).
                "contractId": member.id.replace("-", ""),
                "startDate": _add_months(today, 12 if yearly else 1).isoformat(),
                "value": plan.amount_cents / 100,
                "description": f"CCPA {plan.name} {'anual' if yearly else 'mensal'}"[:35],
                "paymentCreationMode": "SUBSCRIPTION",
                # Up to three attempts within seven days when a debit fails —
                # an empty account on the due date should not end a membership.
                "retryPolicy": "ALLOW_THREE_IN_SEVEN_DAYS",
                "immediateQrCode": {
                    "value": plan.amount_cents / 100,
                    "expirationSeconds": PIX_QR_SECONDS,
                    "description": f"Associação CCPA — {plan.name}"[:35],
                },
            },
        )
        if response.is_error:
            raise _asaas_error(response, "POST /pix/automatic/authorizations")
        body = response.json()
        qr = body.get("immediateQrCode") or {}
        if not body.get("id") or not qr.get("payload"):
            raise PaymentError(
                GENERIC_PAYMENT_ERROR, f"Asaas authorization without QR code: {body}"
            )
        expires = None
        if qr.get("expirationDate"):
            try:
                expires = datetime.fromisoformat(str(qr["expirationDate"])[:19])
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=_BRT).astimezone(UTC)
            except ValueError:
                expires = None
        return CheckoutLink(
            url=None,
            external_reference=body["id"],
            provider=self.name,
            method=PIX_AUTOMATIC,
            customer_id=customer_id,
            mandate_status="PENDING",
            pix_qr_image=qr.get("encodedImage"),
            pix_qr_payload=qr["payload"],
            pix_qr_expires_at=expires
            or datetime.now(UTC) + timedelta(seconds=PIX_QR_SECONDS),
        )

    def _open_subscription(
        self,
        client: httpx.Client,
        member: Member,
        plan: Plan,
        customer_id: str,
        billing_type: str = CARD,
    ) -> CheckoutLink:
        subscription = client.post(
            "/subscriptions",
            json={
                "customer": customer_id,
                # Never UNDEFINED: that would let the member pick boleto at
                # checkout, which the association does not accept.
                "billingType": billing_type,
                "value": plan.amount_cents / 100,
                "cycle": "MONTHLY" if plan.cycle == "MONTHLY" else "YEARLY",
                "description": f"Associação CCPA — {plan.name}",
                "nextDueDate": (
                    date.today() + timedelta(days=FIRST_DUE_DATE_DAYS)
                ).isoformat(),
                "externalReference": member.id,
            },
        )
        if subscription.is_error:
            raise _asaas_error(subscription, "POST /subscriptions")
        sub = subscription.json()

        # A subscription is a recurrence, not a charge: the payable invoice
        # is its first payment, and only that carries the checkout URL.
        charges = client.get("/payments", params={"subscription": sub["id"]})
        if charges.is_error:
            raise _asaas_error(charges, "GET /payments")
        items = charges.json().get("data", [])
        url = items[0]["invoiceUrl"] if items else sub.get("invoiceUrl", "")

        if not url:
            raise PaymentError(GENERIC_PAYMENT_ERROR, "Asaas returned no checkout URL")

        return CheckoutLink(
            url=url,
            external_reference=sub["id"],
            provider=self.name,
            method=billing_type,
            customer_id=customer_id,
        )

    # ---- changing the payment method ----------------------------------
    OPEN_INVOICE = ("PENDING", "OVERDUE")

    def change_method(
        self, member: Member, plan: Plan, payment, new_method: str
    ) -> CheckoutLink:
        if not settings.asaas_api_key:
            raise PaymentError(
                "A chave da Asaas não está configurada.",
                "ASAAS_API_KEY is not configured",
            )
        if not payment.external_reference:
            raise PaymentError(
                "Este cadastro não tem assinatura na Asaas para alterar.",
                "payment without external_reference",
            )
        try:
            with self._client() as client:
                if (
                    payment.method in SUBSCRIPTION_METHODS
                    and new_method in SUBSCRIPTION_METHODS
                ):
                    return self._switch_billing_type(client, payment, new_method)
                return self._replace_charge(client, member, plan, payment, new_method)
        except PaymentError:
            raise
        except httpx.HTTPError as error:
            raise PaymentError(
                "Não foi possível falar com a Asaas agora. Nada foi alterado; "
                "tente de novo em alguns minutos.",
                f"Asaas unreachable: {error!r}",
            ) from error

    def _switch_billing_type(
        self, client: httpx.Client, payment, new_method: str
    ) -> CheckoutLink:
        """Card <-> Pix on the same subscription.

        `updatePendingPayments` carries the change to invoices already
        issued, so the open invoice's page starts offering the new method
        at the same address. Invoices it did not reach (an overdue one, for
        instance) are updated one by one, best effort.
        """
        sub_id = payment.external_reference
        updated = client.put(
            f"/subscriptions/{sub_id}",
            json={"billingType": new_method, "updatePendingPayments": True},
        )
        if updated.is_error:
            raise _asaas_error(updated, "PUT /subscriptions/{id}")
        customer_id = updated.json().get("customer") or payment.provider_customer_id

        url = None
        invoices = client.get("/payments", params={"subscription": sub_id})
        if not invoices.is_error:
            open_ones = sorted(
                (
                    i
                    for i in invoices.json().get("data", [])
                    if i.get("status") in self.OPEN_INVOICE
                ),
                key=lambda i: i.get("dueDate") or "",
            )
            for invoice in open_ones:
                if invoice.get("billingType") != new_method:
                    fixed = client.put(
                        f"/payments/{invoice['id']}",
                        json={
                            "billingType": new_method,
                            "value": invoice.get("value"),
                            "dueDate": invoice.get("dueDate"),
                        },
                    )
                    if fixed.is_error:
                        log.warning(
                            "Invoice %s kept its billing type: %s",
                            invoice["id"],
                            fixed.text[:300],
                        )
            if open_ones:
                url = open_ones[0].get("invoiceUrl")

        return CheckoutLink(
            url=url,  # None: nothing open right now; the old link is kept
            external_reference=sub_id,
            provider=self.name,
            method=new_method,
            customer_id=customer_id,
        )

    def _replace_charge(
        self, client: httpx.Client, member: Member, plan: Plan, payment, new_method: str
    ) -> CheckoutLink:
        """In or out of Pix Automático: open the new charge, then close the old.

        In that order, so a refusal from Asaas leaves the member with the
        charge they already had instead of with none. If closing the old one
        fails, the new one is closed again, so the member is never charged
        twice.
        """
        old_ref = payment.external_reference
        old_is_authorization = payment.method == PIX_AUTOMATIC
        customer_id = payment.provider_customer_id
        if not customer_id and not old_is_authorization:
            found = client.get(f"/subscriptions/{old_ref}")
            if not found.is_error:
                customer_id = found.json().get("customer")
        if not customer_id:
            customer_id = self._create_customer(client, member)

        if new_method == PIX_AUTOMATIC:
            link = self._open_pix_automatic(client, member, plan, customer_id)
        else:
            link = self._open_subscription(
                client, member, plan, customer_id, billing_type=new_method
            )

        if old_is_authorization:
            closed = client.delete(f"/pix/automatic/authorizations/{old_ref}")
            gone = not closed.is_error or closed.status_code in (400, 404)
        else:
            closed = client.delete(f"/subscriptions/{old_ref}")
            gone = not closed.is_error or closed.status_code == 404
        if not gone:
            if new_method == PIX_AUTOMATIC:
                client.delete(f"/pix/automatic/authorizations/{link.external_reference}")
            else:
                client.delete(f"/subscriptions/{link.external_reference}")
            raise _asaas_error(closed, "closing the previous charge")
        return link

    def cancel_pix_authorization(
        self, authorization_id: str, customer_id: str | None
    ) -> str:
        """Cancel a Pix Automático authorization (and remove the customer).

        Cancelling the authorization is what stops the debits; the subscription
        Asaas created from it goes with it.
        """
        if not settings.asaas_api_key:
            raise PaymentError(
                "A chave da Asaas não está configurada; a autorização Pix não "
                "pôde ser cancelada.",
                "ASAAS_API_KEY is not configured",
            )
        try:
            with self._client() as client:
                removed = client.delete(
                    f"/pix/automatic/authorizations/{authorization_id}"
                )
                # 404: never existed / already gone. 400: already cancelled,
                # refused or expired — nothing left to stop.
                if removed.is_error and removed.status_code not in (400, 404):
                    raise _asaas_error(removed, "DELETE /pix/automatic/authorizations")
                note = "autorização Pix Automático cancelada na Asaas"
                if customer_id:
                    gone = client.delete(f"/customers/{customer_id}")
                    note += (
                        " (cliente também)"
                        if not gone.is_error
                        else " (cliente não pôde ser removido)"
                    )
                return note
        except httpx.HTTPError as error:
            raise PaymentError(
                "Não foi possível falar com a Asaas agora. Nada foi excluído; "
                "tente de novo em alguns minutos.",
                f"Asaas unreachable: {error!r}",
            ) from error

    def cancel_subscription(self, subscription_id: str) -> str:
        """Remove a subscription, its pending invoices and its customer.

        Used when a record is deleted from the panel: a test signup left in
        Asaas keeps generating invoices every month. Returns a short note for
        the audit trail. Raises PaymentError when Asaas refuses, so the caller
        can keep the local record instead of orphaning a live subscription.
        """
        if not settings.asaas_api_key:
            raise PaymentError(
                "A chave da Asaas não está configurada; a assinatura não pôde "
                "ser cancelada.",
                "ASAAS_API_KEY is not configured",
            )
        try:
            with self._client() as client:
                found = client.get(f"/subscriptions/{subscription_id}")
                if found.status_code == 404:
                    return "assinatura já não existia na Asaas"
                if found.is_error:
                    raise _asaas_error(found, "GET /subscriptions/{id}")
                customer_id = found.json().get("customer")

                removed = client.delete(f"/subscriptions/{subscription_id}")
                if removed.is_error and removed.status_code != 404:
                    raise _asaas_error(removed, "DELETE /subscriptions/{id}")

                note = "assinatura removida na Asaas"
                if customer_id:
                    gone = client.delete(f"/customers/{customer_id}")
                    # The subscription is what charges; a customer left
                    # behind is harmless, so this failing is only noted.
                    note += (
                        " (cliente também)"
                        if not gone.is_error
                        else " (cliente não pôde ser removido)"
                    )
                return note
        except httpx.HTTPError as error:
            raise PaymentError(
                "Não foi possível falar com a Asaas agora. Nada foi excluído; "
                "tente de novo em alguns minutos.",
                f"Asaas unreachable: {error!r}",
            ) from error

    def verify_webhook(self, headers: dict, body: bytes) -> bool:  # noqa: ARG002
        token = headers.get("asaas-access-token", "")
        if not settings.asaas_webhook_token:
            log.warning("ASAAS_WEBHOOK_TOKEN is empty — webhook not verified.")
            # Tolerated in development, never in production: an unverified
            # webhook lets anyone mark a membership as paid.
            return not settings.is_prod
        return hmac.compare_digest(token, settings.asaas_webhook_token)

    def parse_event(self, payload: dict) -> tuple[str | None, str | None]:
        event = payload.get("event", "")
        payment = payload.get("payment", {}) or {}
        reference = payment.get("subscription") or payment.get("externalReference")
        return reference, self.STATUS_MAP.get(event)


_PROVIDERS: dict[str, type[PaymentProvider]] = {
    "mock": MockProvider,
    "asaas": AsaasProvider,
}


def get_payment_provider() -> PaymentProvider:
    return _PROVIDERS.get(settings.payment_provider, MockProvider)()

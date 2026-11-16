"""Pix Automático: signup, QR code, bank events, renewal, reconciliation."""

import json
import os
import tempfile
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

os.environ.setdefault("APP_ENV", "development")
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{tempfile.gettempdir()}/ccpa_test.db"
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["RESEND_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Charge, Member, MemberStatus, Payment, PaymentStatus  # noqa: E402
from app.services import billing  # noqa: E402
from app.services.payments import AsaasProvider, _add_months  # noqa: E402

PF = {
    "plan_code": "ESTUDANTE",
    "name": "Maria da Silva",
    "mobile": "(91) 98888-7777",
    "email": "maria@exemplo.com.br",
    "login": "maria.silva",
    "password": "senha1234",
    "password_confirmation": "senha1234",
    "accepted_terms": "on",
    "payment_method": "PIX_AUTOMATIC",
}


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def pix_on(monkeypatch):
    monkeypatch.setattr(settings, "pix_automatic_enabled", True)


def the(model):
    with Session(engine) as db:
        return db.scalars(select(model)).one()


# ------------------------------- the form ---------------------------------
def test_option_is_hidden_until_enabled(client):
    page = client.get("/associe-se").text
    assert "Pix Automático" not in page
    assert page.count('name="payment_method" value="CREDIT_CARD"') == 3


def test_option_appears_when_enabled(client, pix_on):
    page = client.get("/associe-se").text
    assert page.count('value="PIX_AUTOMATIC"') == 3  # one per form


def test_pix_is_refused_while_disabled(client):
    response = client.post("/associe-se/pf", data=PF)
    assert response.status_code == 422
    assert "Forma de pagamento indisponível" in response.text


def test_card_is_still_the_default(client, pix_on):
    data = {k: v for k, v in PF.items() if k != "payment_method"}
    assert client.post("/associe-se/pf", data=data).status_code == 303
    assert the(Payment).method == "CREDIT_CARD"


# ------------------------------ mock flow ---------------------------------
def test_pix_signup_shows_the_code_on_our_own_page(client, pix_on):
    response = client.post("/associe-se/pf", data=PF)
    assert response.status_code == 303
    payment = the(Payment)
    member = the(Member)
    assert payment.method == "PIX_AUTOMATIC"
    assert payment.mandate_status == "PENDING"
    # The "checkout" is the success page: the e-mail, WhatsApp and the panel's
    # copy button all lead somewhere the member can pay.
    assert payment.checkout_url.endswith(f"/associe-se/sucesso/{member.id}")

    page = client.get(response.headers["location"]).text
    assert "Pix copia e cola" in page
    assert payment.pix_qr_payload in page
    assert "Autorize o Pix Automático" in page
    assert "Cadastrar meu cartão" not in page


def test_simulated_payment_activates_member_and_mandate(client, pix_on):
    client.post("/associe-se/pf", data=PF)
    member = the(Member)
    client.post(f"/pagamento/simulado/{member.id}/confirmar")
    assert the(Member).status == MemberStatus.ACTIVE
    assert the(Payment).mandate_status == "ACTIVE"
    page = client.get(f"/associe-se/sucesso/{member.id}").text
    assert "Débito automático autorizado" in page


# ------------------------------ Asaas calls -------------------------------
@pytest.fixture
def asaas(monkeypatch, pix_on):
    monkeypatch.setattr(settings, "payment_provider", "asaas")
    monkeypatch.setattr(settings, "asaas_api_key", "test-key")
    monkeypatch.setattr(settings, "asaas_webhook_token", "")
    calls = []

    def install(handler):
        def recording(request):
            body = json.loads(request.content) if request.content else None
            calls.append((request.method, request.url.path, body))
            return handler(request)

        monkeypatch.setattr(
            AsaasProvider,
            "_client",
            lambda self: httpx.Client(
                base_url="https://api.asaas.test/v3",
                transport=httpx.MockTransport(recording),
            ),
        )
        return calls

    return install


def happy_asaas(request):
    if request.url.path.endswith("/customers"):
        return httpx.Response(200, json={"id": "cus_1"})
    if request.url.path.endswith("/pix/automatic/authorizations"):
        return httpx.Response(
            200,
            json={
                "id": "auth_1",
                "status": "PENDING_IMMEDIATE_CHARGE",
                "immediateQrCode": {
                    "encodedImage": "iVBORw0KGgo=",
                    "payload": "00020126PIXCODE",
                    "expirationDate": "2099-01-01 12:00:00",
                },
            },
        )
    return httpx.Response(200, json={"deleted": True})


def test_authorization_is_opened_one_period_ahead(client, asaas):
    calls = asaas(happy_asaas)
    assert client.post("/associe-se/pf", data=PF).status_code == 303

    method, path, body = calls[1]
    assert (method, path) == ("POST", "/v3/pix/automatic/authorizations")
    assert body["customerId"] == "cus_1"
    assert body["frequency"] == "MONTHLY"
    assert body["paymentCreationMode"] == "SUBSCRIPTION"
    assert body["retryPolicy"] == "ALLOW_THREE_IN_SEVEN_DAYS"
    assert body["value"] == 29.90
    assert body["immediateQrCode"]["value"] == 29.90
    assert len(body["contractId"]) <= 35
    assert len(body["description"]) <= 35
    # The QR code pays the first month; recurring debits start a month later,
    # or the first month would be charged twice.
    assert body["startDate"] == _add_months(date.today(), 1).isoformat()

    payment = the(Payment)
    assert payment.external_reference == "auth_1"
    assert payment.provider_customer_id == "cus_1"
    assert payment.pix_qr_image == "iVBORw0KGgo="
    page = client.get(f"/associe-se/sucesso/{the(Member).id}").text
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in page


def test_annual_plan_is_an_annual_authorization(client, asaas):
    calls = asaas(happy_asaas)
    client.post("/associe-se/pf", data={**PF, "plan_code": "ESTUDANTE_ANUAL"})
    body = calls[1][2]
    assert body["frequency"] == "ANNUALLY"
    assert body["value"] == 328.90
    assert body["startDate"] == _add_months(date.today(), 12).isoformat()


def test_account_not_enabled_for_pix_is_explained(client, asaas):
    def not_enabled(request):
        if request.url.path.endswith("/customers"):
            return httpx.Response(200, json={"id": "cus_1"})
        return httpx.Response(
            400,
            json={"errors": [{"description": "Sua conta não possui Pix Automático."}]},
        )

    asaas(not_enabled)
    response = client.post("/associe-se/pf", data=PF)
    assert response.status_code == 502
    assert "Sua conta não possui Pix Automático." in response.text
    with Session(engine) as db:
        assert db.scalars(select(Member)).first() is None


def test_month_arithmetic_clamps_short_months():
    assert _add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert _add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert _add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)
    assert _add_months(date(2026, 3, 31), 12) == date(2027, 3, 31)


# ------------------------------ bank events -------------------------------
def signed_up(client, asaas):
    asaas(happy_asaas)
    client.post("/associe-se/pf", data=PF)
    return the(Member).id


def hook(client, payload):
    response = client.post("/webhooks/pagamento", json=payload)
    assert response.status_code == 200
    return response.json()


def test_first_payment_is_matched_by_customer_and_activates(client, asaas):
    signed_up(client, asaas)
    hook(
        client,
        {
            "event": "PAYMENT_RECEIVED",
            "payment": {
                "id": "pay_1",
                "customer": "cus_1",
                "value": 29.9,
                "status": "RECEIVED",
                "dueDate": date.today().isoformat(),
                "paymentDate": date.today().isoformat(),
            },
        },
    )
    assert the(Member).status == MemberStatus.ACTIVE
    assert the(Charge).provider_id == "pay_1"


def authorization(event, status):
    return {"event": event, "authorization": {"id": "auth_1", "status": status}}


def test_activation_is_recorded(client, asaas):
    signed_up(client, asaas)
    hook(
        client, authorization("PIX_AUTOMATIC_RECURRING_AUTHORIZATION_ACTIVATED", "ACTIVE")
    )
    assert the(Payment).mandate_status == "ACTIVE"


def test_payer_cancelling_in_the_bank_cancels_an_active_member(client, asaas):
    member_id = signed_up(client, asaas)
    client.post(f"/pagamento/simulado/{member_id}/confirmar")  # noop outside mock
    with Session(engine) as db:
        db.get(Member, member_id).status = MemberStatus.ACTIVE
        db.scalars(select(Payment)).one().status = PaymentStatus.CONFIRMED
        db.commit()
    hook(
        client,
        authorization("PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CANCELLED", "CANCELLED"),
    )
    assert the(Member).status == MemberStatus.CANCELLED


def test_refused_before_paying_offers_a_new_qr_code(client, asaas):
    member_id = signed_up(client, asaas)
    hook(
        client, authorization("PIX_AUTOMATIC_RECURRING_AUTHORIZATION_REFUSED", "REFUSED")
    )
    assert the(Member).status == MemberStatus.AWAITING_PAYMENT
    page = client.get(f"/associe-se/sucesso/{member_id}").text
    assert "Gerar novo QR Code" in page
    assert "Pix copia e cola" not in page


def test_expired_code_is_renewed_and_the_old_one_cancelled(client, asaas):
    member_id = signed_up(client, asaas)
    with Session(engine) as db:
        db.scalars(select(Payment)).one().pix_qr_expires_at = datetime.now(
            UTC
        ) - timedelta(minutes=1)
        db.commit()
    assert "Gerar novo QR Code" in client.get(f"/associe-se/sucesso/{member_id}").text

    calls = asaas(happy_asaas)
    calls.clear()
    response = client.post(f"/associe-se/sucesso/{member_id}/novo-pix")
    assert response.headers["location"].endswith("?novo=1")
    assert calls[0][:2] == ("DELETE", "/v3/pix/automatic/authorizations/auth_1")
    assert calls[1][:2] == ("POST", "/v3/pix/automatic/authorizations")
    assert calls[1][2]["customerId"] == "cus_1"  # the customer is reused
    page = client.get(f"/associe-se/sucesso/{member_id}?novo=1").text
    assert "Pix copia e cola" in page


def test_a_payable_code_is_never_replaced(client, asaas):
    member_id = signed_up(client, asaas)
    calls = asaas(happy_asaas)
    calls.clear()
    client.post(f"/associe-se/sucesso/{member_id}/novo-pix")
    assert calls == []


# ---------------------------- reconciliation ------------------------------
def test_reconcile_finds_pix_payments_by_customer():
    with Session(engine) as db:
        member = Member(
            entity_type="PF", plan_code="ESTUDANTE", name="Ana", email="a@x.com",
            login="ana", password_hash="x", status=MemberStatus.AWAITING_PAYMENT,
        )  # fmt: skip
        db.add(member)
        db.flush()
        db.add(
            Payment(
                member_id=member.id,
                provider="asaas",
                external_reference="auth_9",
                amount_cents=2990,
                method="PIX_AUTOMATIC",
                provider_customer_id="cus_9",
            )  # fmt: skip
        )
        db.commit()

    def handler(request):
        path = request.url.path
        if path.endswith("/subscriptions"):
            return httpx.Response(200, json={"data": [], "hasMore": False})
        if path.endswith("/payments"):
            invoice = {
                "id": "pay_9", "customer": "cus_9", "value": 29.9,
                "status": "RECEIVED", "dueDate": "2026-09-01",
            }  # fmt: skip
            return httpx.Response(200, json={"data": [invoice], "hasMore": False})
        if path.endswith("/pix/automatic/authorizations/auth_9"):
            return httpx.Response(200, json={"id": "auth_9", "status": "ACTIVE"})
        return httpx.Response(404)

    client = httpx.Client(
        base_url="https://api.asaas.test/v3", transport=httpx.MockTransport(handler)
    )
    with Session(engine) as db:
        report = billing.reconcile(db, client=client)
        assert report.error is None and not report.not_found
    assert the(Member).status == MemberStatus.ACTIVE
    assert the(Payment).mandate_status == "ACTIVE"


# ------------------------------- deletion ---------------------------------
def test_deleting_a_pix_member_cancels_the_authorization(client, asaas):
    from app.models import AdminRole, AdminUser
    from app.security import hash_password

    member_id = signed_up(client, asaas)
    with Session(engine) as db:
        db.add(
            AdminUser(
                name="Leo",
                email="leo@ccpa.org",
                role=AdminRole.ADMIN,
                password_hash=hash_password("senha-forte-123"),
                must_change_password=False,
            )  # fmt: skip
        )
        db.commit()
    client.get("/painel/entrar")
    csrf = client.cookies.get("ccpa_painel_csrf")
    client.post(
        "/painel/entrar",
        data={"email": "leo@ccpa.org", "password": "senha-forte-123", "csrf": csrf},
    )
    calls = asaas(happy_asaas)
    calls.clear()
    response = client.post(
        f"/painel/associados/{member_id}/excluir",
        data={"motivo": "teste", "confirmacao": "EXCLUIR", "csrf": csrf},
    )
    assert response.status_code == 303
    assert calls[0][:2] == ("DELETE", "/v3/pix/automatic/authorizations/auth_1")
    assert calls[1][:2] == ("DELETE", "/v3/customers/cus_1")

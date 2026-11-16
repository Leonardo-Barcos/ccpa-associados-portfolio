"""Management panel: access control, figures, export, WhatsApp, reconciliation."""

import os
import tempfile
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

os.environ.setdefault("APP_ENV", "development")
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{tempfile.gettempdir()}/ccpa_test.db"
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["RESEND_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AdminRole,
    AdminUser,
    AuditLog,
    Charge,
    EntityType,
    Member,
    MemberStatus,
    Payment,
    PaymentStatus,
)
from app.security import (  # noqa: E402
    SESSION_MAX_AGE,
    create_session_token,
    hash_password,
    read_session_token,
)
from app.services import billing  # noqa: E402
from app.services import painel as svc  # noqa: E402

PASSWORD = "senha-forte-123"


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


def make_user(
    email="leo@ccpa.org", role=AdminRole.ADMIN, must_change=False, name="Leonardo"
) -> str:
    with Session(engine) as db:
        user = AdminUser(
            name=name,
            email=email,
            role=role,
            password_hash=hash_password(PASSWORD),
            must_change_password=must_change,
        )
        db.add(user)
        db.commit()
        return user.id


def csrf(client) -> str:
    client.get("/painel/entrar")
    return client.cookies.get("ccpa_painel_csrf")


def login(client, email="leo@ccpa.org", password=PASSWORD):
    return client.post(
        "/painel/entrar",
        data={"email": email, "password": password, "csrf": csrf(client)},
    )


def make_member(
    name="Maria da Silva",
    status=MemberStatus.AWAITING_PAYMENT,
    plan="PROFISSIONAL",
    provider="mock",
    reference="mock_1",
    mobile="91988887777",
    **extra,
) -> str:
    with Session(engine) as db:
        member = Member(
            entity_type=EntityType.PF,
            plan_code=plan,
            status=status,
            name=name,
            document=extra.pop("document", "11144477735"),
            email=extra.pop("email", f"{name.split()[0].lower()}@exemplo.com"),
            mobile=mobile,
            login=extra.pop("login", name.split()[0].lower()),
            password_hash="x",
            accepted_terms=True,
            chapter=extra.pop("chapter", "Belém"),
            **extra,
        )
        db.add(member)
        db.flush()
        db.add(
            Payment(
                member_id=member.id,
                provider=provider,
                external_reference=reference,
                amount_cents=4490,
                cycle="MONTHLY",
                checkout_url="https://www.asaas.com/i/abc123",
            )
        )
        db.commit()
        return member.id


# ------------------------------ session -----------------------------------
def test_session_token_round_trip_and_tampering():
    token = create_session_token("user-1", 3, now=1_000_000)
    assert read_session_token(token, now=1_000_100) == ("user-1", 3)
    assert read_session_token(token, now=1_000_000 + SESSION_MAX_AGE + 1) is None
    forged = token.replace("user-1", "user-2")
    assert read_session_token(forged, now=1_000_100) is None
    assert read_session_token("lixo") is None


# --------------------------- access control -------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/painel",
        "/painel/associados",
        "/painel/associados/exportar.csv",
        "/painel/usuarios",
        "/painel/registro",
    ],
)
def test_every_page_requires_login(client, path):
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/painel/entrar")


def test_login_then_dashboard(client):
    make_user()
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/painel"
    page = client.get("/painel")
    assert page.status_code == 200
    assert "Visão geral" in page.text
    assert page.headers["cache-control"] == "no-store"
    assert "noindex" in page.headers["x-robots-tag"]


def test_login_without_csrf_is_refused(client):
    make_user()
    response = client.post(
        "/painel/entrar", data={"email": "leo@ccpa.org", "password": PASSWORD}
    )
    assert response.status_code == 403


def test_wrong_password_gives_one_generic_message(client):
    make_user()
    wrong = login(client, password="errada")
    unknown = login(client, email="ninguem@ccpa.org")
    assert wrong.status_code == unknown.status_code == 401
    assert "E-mail ou senha incorretos" in wrong.text
    assert "E-mail ou senha incorretos" in unknown.text


def test_five_failures_lock_the_account_even_for_the_right_password(client):
    make_user()
    for _ in range(5):
        login(client, password="errada")
    response = login(client)
    assert response.status_code == 429
    assert "Aguarde 15 minutos" in response.text


def test_login_never_redirects_outside_the_panel(client):
    make_user()
    token = csrf(client)
    response = client.post(
        "/painel/entrar",
        data={
            "email": "leo@ccpa.org",
            "password": PASSWORD,
            "csrf": token,
            "proxima": "https://golpe.example/painel",
        },
    )
    assert response.headers["location"] == "/painel"


def test_provisional_password_must_be_changed_first(client):
    make_user(must_change=True)
    assert login(client).headers["location"] == "/painel/senha"
    assert client.get("/painel").headers["location"] == "/painel/senha"

    old_cookie = client.cookies.get("ccpa_painel")
    response = client.post(
        "/painel/senha",
        data={
            "atual": PASSWORD,
            "nova": "uma frase bem longa",
            "confirmacao": "uma frase bem longa",
            "csrf": client.cookies.get("ccpa_painel_csrf"),
        },
    )
    assert response.status_code == 303
    assert client.get("/painel").status_code == 200

    # The session opened before the change is no longer accepted.
    client.cookies.set("ccpa_painel", old_cookie, path="/painel")
    assert client.get("/painel").status_code == 303


def test_team_role_cannot_export_or_manage_users(client):
    make_user(email="elias@ccpa.org", role=AdminRole.EQUIPE, name="Elias")
    login(client, email="elias@ccpa.org")
    assert client.get("/painel/associados").status_code == 200
    assert client.get("/painel/associados/exportar.csv").status_code == 403
    assert client.get("/painel/usuarios").status_code == 403
    assert client.get("/painel/registro").status_code == 403
    assert "Exportar planilha" not in client.get("/painel/associados").text


def test_deactivating_a_user_ends_their_session(client):
    admin_id = make_user()
    team_id = make_user(email="elias@ccpa.org", role=AdminRole.EQUIPE, name="Elias")

    with TestClient(app, follow_redirects=False) as elias:
        login(elias, email="elias@ccpa.org")
        assert elias.get("/painel").status_code == 200

        login(client)
        client.post(
            f"/painel/usuarios/{team_id}/ativo",
            data={"csrf": client.cookies.get("ccpa_painel_csrf")},
        )
        assert elias.get("/painel").status_code == 303

    # And nobody can deactivate themselves.
    client.post(
        f"/painel/usuarios/{admin_id}/ativo",
        data={"csrf": client.cookies.get("ccpa_painel_csrf")},
    )
    with Session(engine) as db:
        assert db.get(AdminUser, admin_id).active


def test_admin_creates_user_with_a_provisional_password(client):
    make_user()
    login(client)
    response = client.post(
        "/painel/usuarios",
        data={
            "name": "Elias",
            "email": "Elias@CCPA.org",
            "role": "EQUIPE",
            "csrf": client.cookies.get("ccpa_painel_csrf"),
        },
    )
    assert response.status_code == 200
    assert "Senha provisória de" in response.text
    with Session(engine) as db:
        created = db.scalar(select(AdminUser).where(AdminUser.email == "elias@ccpa.org"))
        assert created.must_change_password
        assert created.role == AdminRole.EQUIPE


# ------------------------------ members -----------------------------------
def test_member_list_search_and_filters(client):
    make_user()
    make_member("Maria da Silva", document="11144477735")
    make_member(
        "Joao Souza",
        plan="ESTUDANTE_ANUAL",
        document="52998224725",
        reference="mock_2",
        chapter="Santarém",
    )
    login(client)

    by_cpf = client.get("/painel/associados", params={"q": "111.444.777-35"}).text
    assert "Maria da Silva" in by_cpf and "Joao Souza" not in by_cpf

    yearly = client.get("/painel/associados", params={"ciclo": "YEARLY"}).text
    assert "Joao Souza" in yearly and "Maria da Silva" not in yearly

    tier = client.get("/painel/associados", params={"plano": "ESTUDANTE"}).text
    assert "Joao Souza" in tier and "Maria da Silva" not in tier

    chapter = client.get("/painel/associados", params={"regional": "Santarém"}).text
    assert "Joao Souza" in chapter and "Maria da Silva" not in chapter


def test_opening_a_member_is_recorded(client):
    make_user()
    member_id = make_member()
    login(client)
    page = client.get(f"/painel/associados/{member_id}")
    assert page.status_code == 200
    assert "111.444.777-35" in page.text  # formatted CPF
    assert "(91) 98888-7777" in page.text
    with Session(engine) as db:
        entry = db.scalar(select(AuditLog).where(AuditLog.action == "viu_ficha"))
        assert entry.target == member_id
        assert entry.user_name == "Leonardo"


def test_whatsapp_charge_carries_the_payment_link(client):
    make_user()
    member_id = make_member()
    login(client)
    response = client.get(f"/painel/associados/{member_id}/whatsapp?tipo=cobranca")
    assert response.status_code == 303
    url = urlparse(response.headers["location"])
    assert url.netloc == "wa.me"
    assert url.path == "/5591988887777"
    text = unquote(parse_qs(url.query)["text"][0])
    assert "Maria" in text
    assert "https://www.asaas.com/i/abc123" in text


def test_member_without_mobile_gets_no_whatsapp(client):
    make_user()
    member_id = make_member(mobile=None)
    login(client)
    page = client.get(f"/painel/associados/{member_id}").text
    assert "Cobrar no WhatsApp" not in page
    assert "cobrança por WhatsApp não está disponível" in page


# ------------------------------- export -----------------------------------
def test_export_is_the_filtered_list_ready_for_excel(client):
    make_user()
    make_member("Maria da Silva", company='=HYPERLINK("http://x")')
    make_member(
        "Joao Souza", status=MemberStatus.ACTIVE, document="52998224725",
        reference="mock_2",
    )  # fmt: skip
    login(client)

    response = client.get("/painel/associados/exportar.csv?status=AWAITING_PAYMENT")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    body = response.content.decode("utf-8")
    assert body.startswith("﻿")  # BOM: accents survive Excel
    header, *rows = body.lstrip("﻿").strip().split("\r\n")
    assert header.split(";")[0] == "Cadastro"
    assert len(rows) == 1 and "Maria da Silva" in rows[0]
    assert "'=HYPERLINK" in rows[0]  # a formula from the public form is defused

    with Session(engine) as db:
        entry = db.scalar(select(AuditLog).where(AuditLog.action == "exportou"))
        assert entry.detail.startswith("1 associado(s)")


# -------------------------- charges & figures -----------------------------
def asaas_member(reference="sub_1", **kw) -> str:
    return make_member(provider="asaas", reference=reference, **kw)


def test_webhook_records_every_monthly_renewal(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "payment_provider", "asaas")
    member_id = asaas_member()

    for pay_id, day in (("pay_1", "2026-07-10"), ("pay_2", "2026-08-10")):
        response = client.post(
            "/webhooks/pagamento",
            json={
                "event": "PAYMENT_CONFIRMED",
                "payment": {
                    "id": pay_id,
                    "subscription": "sub_1",
                    "value": 44.90,
                    "status": "CONFIRMED",
                    "dueDate": day,
                    "confirmedDate": day,
                },
            },
        )
        assert response.status_code == 200

    with Session(engine) as db:
        charges = db.scalars(select(Charge).where(Charge.member_id == member_id)).all()
        assert sorted(c.provider_id for c in charges) == ["pay_1", "pay_2"]
        assert all(c.amount_cents == 4490 for c in charges)
        assert db.get(Member, member_id).status == MemberStatus.ACTIVE


def test_mock_checkout_counts_as_revenue(client):
    member_id = make_member()
    client.post(f"/pagamento/simulado/{member_id}/confirmar")
    with Session(engine) as db:
        figures = svc.overview(db)
    assert figures["counts"]["ativos"] == 1
    assert figures["recebido_mes"] == "R$ 44,90"
    assert figures["recorrente"] == "R$ 44,90"
    assert figures["conversao"] == "100%"


def test_recurring_revenue_spreads_annual_plans():
    make_member("Ana Lima", status=MemberStatus.ACTIVE, plan="ESTUDANTE_ANUAL")
    with Session(engine) as db:
        # 328,90 / 12 = 27,41
        assert svc.overview(db)["recorrente"] == "R$ 27,41"


# ------------------------------- status -----------------------------------
def charge(status, due):
    return Charge(provider_id=f"p_{status}_{due}", status=status, due_date=due)


def test_derive_status_rules():
    derive = billing.derive_status
    assert derive([]) == MemberStatus.AWAITING_PAYMENT
    assert derive([charge("PENDING", "2026-09-01")]) == MemberStatus.AWAITING_PAYMENT
    assert derive([charge("CONFIRMED", "2026-08-01")]) == MemberStatus.ACTIVE
    assert (
        derive([charge("RECEIVED", "2026-08-01"), charge("OVERDUE", "2026-09-01")])
        == MemberStatus.OVERDUE
    )
    assert (
        derive([charge("CONFIRMED", "2026-08-01"), charge("REFUNDED", "2026-09-01")])
        == MemberStatus.CANCELLED
    )
    assert (
        derive([charge("CONFIRMED", "2026-08-01")], subscription_deleted=True)
        == MemberStatus.CANCELLED
    )


# --------------------------- reconciliation -------------------------------
def fake_asaas(subscriptions, payments):
    def handler(request: httpx.Request) -> httpx.Response:
        data = subscriptions if request.url.path.endswith("/subscriptions") else payments
        return httpx.Response(200, json={"data": data, "hasMore": False})

    return httpx.Client(
        base_url="https://api.asaas.test/v3", transport=httpx.MockTransport(handler)
    )


def test_reconcile_activates_a_member_whose_webhook_was_lost():
    paid = asaas_member("sub_paid", name="Paga Silva", document="52998224725")
    refunded = asaas_member(
        "sub_refund", name="Estorno Souza", status=MemberStatus.ACTIVE,
        document="11144477735",
    )  # fmt: skip
    missing = asaas_member("sub_gone", name="Sumida Costa", document="39053344705")

    client = fake_asaas(
        [{"id": "sub_paid", "deleted": False}, {"id": "sub_refund", "deleted": False}],
        [
            {
                "id": "pay_a", "subscription": "sub_paid", "value": 44.9,
                "status": "CONFIRMED", "dueDate": "2026-09-01",
                "confirmedDate": "2026-09-02",
            },
            {
                "id": "pay_b", "subscription": "sub_refund", "value": 44.9,
                "status": "REFUNDED", "dueDate": "2026-09-01",
            },
            {"id": "pay_x", "subscription": "sub_de_outro_sistema", "status": "CONFIRMED"},
        ],
    )  # fmt: skip

    with Session(engine) as db:
        report = billing.reconcile(db, client=client)
        assert report.error is None
        assert report.checked == 3
        assert report.charges_created == 2
        changed = {c.member.id: (c.before, c.after) for c in report.changes}
        assert changed[paid] == (MemberStatus.AWAITING_PAYMENT, MemberStatus.ACTIVE)
        assert changed[refunded] == (MemberStatus.ACTIVE, MemberStatus.CANCELLED)
        assert [m.id for m in report.not_found] == [missing]

        payment = db.scalar(select(Payment).where(Payment.member_id == paid))
        assert payment.status == PaymentStatus.CONFIRMED
        assert payment.paid_at is not None

    # Running it again changes nothing and counts nothing twice.
    with Session(engine) as db:
        again = billing.reconcile(db, client=client)
        assert again.changes == []
        assert again.charges_created == 0
        assert len(db.scalars(select(Charge)).all()) == 2


def test_reconcile_explains_itself_without_asaas():
    with Session(engine) as db:
        report = billing.reconcile(db)
    assert "PAYMENT_PROVIDER=asaas" in report.error


# ------------------------- canonical domain -------------------------------
def test_production_vercel_app_address_redirects_to_the_club_domain(client, monkeypatch):
    from app.config import settings

    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setattr(settings, "base_url", "https://associados.ccpara.com.br")
    vercel = {"host": "ccpa-membros.vercel.app"}

    response = client.get("/associe-se?tipo=pj", headers=vercel)
    assert response.status_code == 308
    assert (
        response.headers["location"]
        == "https://associados.ccpara.com.br/associe-se?tipo=pj"
    )
    # The club's own domain is served normally...
    ours = client.get("/associe-se", headers={"host": "associados.ccpara.com.br"})
    assert ours.status_code == 200
    # ...and the webhook is never redirected: Asaas does not follow redirects.
    hook = client.post("/webhooks/pagamento", json={}, headers=vercel)
    assert hook.status_code != 308


def test_preview_deployments_are_not_redirected(client, monkeypatch):
    from app.config import settings

    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setattr(settings, "base_url", "https://associados.ccpara.com.br")
    response = client.get(
        "/associe-se", headers={"host": "ccpa-membros-git-main.vercel.app"}
    )
    assert response.status_code == 200


# ------------------------------- deletion ---------------------------------
def delete(client, member_id, motivo="cadastro de teste", confirmacao="EXCLUIR"):
    return client.post(
        f"/painel/associados/{member_id}/excluir",
        data={
            "motivo": motivo,
            "confirmacao": confirmacao,
            "csrf": client.cookies.get("ccpa_painel_csrf"),
        },
    )


def count(model) -> int:
    with Session(engine) as db:
        return len(db.scalars(select(model)).all())


def test_team_role_cannot_delete(client):
    make_user(email="elias@ccpa.org", role=AdminRole.EQUIPE, name="Elias")
    member_id = make_member()
    login(client, email="elias@ccpa.org")
    assert client.get(f"/painel/associados/{member_id}/excluir").status_code == 403
    assert delete(client, member_id).status_code == 403
    assert count(Member) == 1


def test_delete_needs_a_reason_and_the_confirmation_word(client):
    make_user()
    member_id = make_member()
    login(client)
    assert delete(client, member_id, motivo="").status_code == 422
    assert delete(client, member_id, confirmacao="sim").status_code == 422
    assert count(Member) == 1


def test_delete_without_csrf_is_refused(client):
    make_user()
    member_id = make_member()
    login(client)
    response = client.post(
        f"/painel/associados/{member_id}/excluir",
        data={"motivo": "teste", "confirmacao": "EXCLUIR"},
    )
    assert response.status_code == 403
    assert count(Member) == 1


def test_admin_deletes_a_test_signup_and_it_is_recorded(client):
    from app.models import EmailLog

    make_user()
    member_id = make_member()
    with Session(engine) as db:
        db.add(EmailLog(member_id=member_id, recipient="m@x.com", template="welcome"))
        db.commit()
    login(client)
    assert "Excluir cadastro" in client.get(f"/painel/associados/{member_id}").text

    response = delete(client, member_id)
    assert response.status_code == 303
    assert "excluido=Maria" in response.headers["location"]
    assert count(Member) == 0
    assert count(Payment) == 0
    assert count(EmailLog) == 0
    with Session(engine) as db:
        entry = db.scalar(select(AuditLog).where(AuditLog.action == "excluiu_associado"))
        assert entry.target == member_id
        assert "cadastro de teste" in entry.detail
        assert entry.user_name == "Leonardo"


def test_a_paying_member_cannot_be_deleted(client):
    make_user()
    member_id = make_member()
    client.post(f"/pagamento/simulado/{member_id}/confirmar")  # pays (mock)
    login(client)
    page = client.get(f"/painel/associados/{member_id}/excluir").text
    assert "pagamento confirmado" in page
    assert delete(client, member_id).status_code == 409
    assert count(Member) == 1


def fake_asaas_client(handler):
    def factory(self):  # noqa: ARG001
        return httpx.Client(
            base_url="https://api.asaas.test/v3",
            transport=httpx.MockTransport(handler),
        )

    return factory


@pytest.fixture
def asaas_key(monkeypatch):
    from app.config import settings
    from app.services.payments import AsaasProvider

    monkeypatch.setattr(settings, "asaas_api_key", "test-key")

    def install(handler):
        monkeypatch.setattr(AsaasProvider, "_client", fake_asaas_client(handler))

    return install


def test_delete_removes_the_subscription_in_asaas(client, asaas_key):
    calls = []

    def handler(request):
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(200, json={"id": "sub_1", "customer": "cus_1"})
        return httpx.Response(200, json={"deleted": True})

    asaas_key(handler)
    make_user()
    member_id = make_member(provider="asaas", reference="sub_1")
    login(client)
    assert delete(client, member_id).status_code == 303
    assert calls == [
        "GET /v3/subscriptions/sub_1",
        "DELETE /v3/subscriptions/sub_1",
        "DELETE /v3/customers/cus_1",
    ]
    assert count(Member) == 0


def test_subscription_already_gone_in_asaas_still_deletes(client, asaas_key):
    asaas_key(lambda request: httpx.Response(404, json={}))
    make_user()
    member_id = make_member(provider="asaas", reference="sub_gone")
    login(client)
    assert delete(client, member_id).status_code == 303
    assert count(Member) == 0


def test_asaas_failure_keeps_the_record(client, asaas_key):
    asaas_key(lambda request: httpx.Response(500, text="boom"))
    make_user()
    member_id = make_member(provider="asaas", reference="sub_1")
    login(client)
    response = delete(client, member_id)
    assert response.status_code == 502
    assert count(Member) == 1  # never a live subscription with no record

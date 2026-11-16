"""End-to-end tests for the signup flow."""

import json as json_module
import os
import tempfile

import pytest

os.environ.setdefault("APP_ENV", "development")
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{tempfile.gettempdir()}/ccpa_test.db"
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["RESEND_API_KEY"] = ""  # keeps the tests from sending real mail

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.validators import is_valid_cnpj, is_valid_cpf  # noqa: E402


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


INDIVIDUAL = {
    "plan_code": "PROFISSIONAL",
    "name": "Maria da Silva Souza",
    "cpf": "111.444.777-35",
    "mobile": "(91) 98888-7777",
    "email": "maria@exemplo.com.br",
    "chapter": "Belém",
    "state": "PA",
    "city": "Belém",
    "postal_code": "66000-000",
    "login": "maria.silva",
    "password": "senha1234",
    "password_confirmation": "senha1234",
    "accepted_terms": "on",
}

COMPANY = {
    "plan_code": "EMPRESA",
    "legal_name": "Agência Criativa Ltda",
    "trade_name": "Agência Criativa",
    "cnpj": "11.222.333/0001-81",
    "chapter": "Santarém",
    "email": "contato@agencia.com.br",
    "login": "agencia.criativa",
    "password": "senha1234",
    "password_confirmation": "senha1234",
    "accepted_terms": "on",
}


# ----------------------------- validators ---------------------------------
def test_cpf_validator():
    assert is_valid_cpf("111.444.777-35")
    assert not is_valid_cpf("111.444.777-00")
    assert not is_valid_cpf("11111111111")  # repeated digits pass the maths


def test_cnpj_validator():
    assert is_valid_cnpj("11.222.333/0001-81")
    assert not is_valid_cnpj("11.222.333/0001-00")


# ------------------------------- pages ------------------------------------
def test_signup_page_serves_both_forms(client):
    response = client.get("/associe-se")
    assert response.status_code == 200
    assert 'action="/associe-se/pf"' in response.text
    assert 'action="/associe-se/pj"' in response.text
    for chapter in ("Belém", "Canaã dos Carajás", "Paragominas", "Santarém"):
        assert chapter in response.text


def test_security_headers_are_present(client):
    response = client.get("/associe-se")
    assert response.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in response.headers


# ----------------------------- happy path ---------------------------------
def test_individual_signup_creates_charge_and_redirects(client):
    response = client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/associe-se/sucesso/")

    page = client.get(location)
    assert page.status_code == 200
    assert "Cadastro recebido" in page.text
    assert "/pagamento/simulado/" in page.text


def test_company_signup(client):
    response = client.post("/associe-se/pj", data=COMPANY, follow_redirects=False)
    assert response.status_code == 303


def test_confirmed_payment_activates_the_member(client):
    response = client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    member_id = response.headers["location"].rsplit("/", 1)[-1]

    client.post(f"/pagamento/simulado/{member_id}/confirmar", follow_redirects=False)
    page = client.get(f"/associe-se/sucesso/{member_id}")
    assert "Associação confirmada" in page.text


# ----------------------------- validation ---------------------------------
@pytest.mark.parametrize(
    "field,value,expected_text",
    [
        ("cpf", "111.444.777-00", "CPF inválido"),
        ("email", "nao-e-email", "E-mail inválido"),
        ("password", "curta1", "Valor muito curto"),
        ("mobile", "123", "Telefone inválido"),
    ],
)
def test_individual_form_rejects_bad_input(client, field, value, expected_text):
    data = {**INDIVIDUAL, field: value}
    if field == "password":
        data["password_confirmation"] = value
    response = client.post("/associe-se/pf", data=data)
    assert response.status_code == 422
    assert expected_text in response.text


def test_password_confirmation_must_match(client):
    response = client.post(
        "/associe-se/pf", data={**INDIVIDUAL, "password_confirmation": "outra1234"}
    )
    assert response.status_code == 422
    assert "não conferem" in response.text


def test_terms_are_mandatory(client):
    data = {k: v for k, v in INDIVIDUAL.items() if k != "accepted_terms"}
    response = client.post("/associe-se/pf", data=data)
    assert response.status_code == 422
    assert "termos de adesão" in response.text


def test_company_form_rejects_bad_cnpj(client):
    response = client.post(
        "/associe-se/pj", data={**COMPANY, "cnpj": "11.222.333/0001-00"}
    )
    assert response.status_code == 422
    assert "CNPJ inválido" in response.text


def test_company_chapter_is_mandatory(client):
    response = client.post("/associe-se/pj", data={**COMPANY, "chapter": ""})
    assert response.status_code == 422


def test_duplicate_email_is_rejected(client):
    client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    response = client.post(
        "/associe-se/pf", data={**INDIVIDUAL, "login": "outro.login", "cpf": ""}
    )
    assert response.status_code == 409
    assert "já existe uma associação" in response.text.lower()


def test_duplicate_login_is_rejected(client):
    client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    response = client.post(
        "/associe-se/pf",
        data={**INDIVIDUAL, "email": "outra@exemplo.com", "cpf": ""},
    )
    assert response.status_code == 409
    assert "login já está em uso" in response.text.lower()


def test_password_is_never_stored_in_plain_text(client):
    from sqlalchemy.orm import Session

    from app.models import Member

    client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    with Session(engine) as db:
        member = db.query(Member).first()
        assert member.password_hash.startswith("$argon2")
        assert INDIVIDUAL["password"] not in member.password_hash


def test_welcome_email_attempt_is_logged(client):
    from sqlalchemy.orm import Session

    from app.models import EmailLog

    client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    with Session(engine) as db:
        entry = db.query(EmailLog).first()
        assert entry is not None
        assert entry.template == "welcome"


def test_webhook_rejects_a_bad_token(monkeypatch, client):
    from app.config import settings

    monkeypatch.setattr(settings, "payment_provider", "asaas")
    monkeypatch.setattr(settings, "asaas_webhook_token", "segredo")
    response = client.post(
        "/webhooks/pagamento",
        json={"event": "PAYMENT_CONFIRMED"},
        headers={"asaas-access-token": "errado"},
    )
    assert response.status_code == 401


def test_empty_optional_fields_do_not_break_the_form(client):
    """Regression: HTML posts "" for untouched fields, which must not be
    parsed as an invalid date or e-mail."""
    data = {
        **INDIVIDUAL,
        "birth_date": "",
        "secondary_email": "",
        "id_card": "",
        "secondary_phone": "",
        "linkedin": "",
        "gender": "",
    }
    response = client.post("/associe-se/pf", data=data, follow_redirects=False)
    assert response.status_code == 303


def test_error_messages_are_in_portuguese(client):
    data = {k: v for k, v in INDIVIDUAL.items() if k != "name"}
    response = client.post("/associe-se/pf", data=data)
    assert "Campo obrigatório" in response.text


# ==========================================================================
#  Credit card through Asaas
# ==========================================================================


def _asaas_client(recorder: dict) -> object:
    """An httpx client that answers the Asaas endpoints without a network."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/customers"):
            return httpx.Response(200, json={"id": "cus_123"})
        if request.url.path.endswith("/subscriptions"):
            recorder["subscription"] = json_module.loads(request.content)
            return httpx.Response(200, json={"id": "sub_123"})
        if request.url.path.endswith("/payments"):
            return httpx.Response(
                200,
                json={"data": [{"invoiceUrl": "https://asaas.com/i/abc123"}]},
            )
        return httpx.Response(404, json={})

    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://api-sandbox.asaas.com/v3",
    )


def test_asaas_subscription_is_credit_card_only(monkeypatch):
    """The association does not accept Pix or boleto.

    `billingType: UNDEFINED` would put both back on the Asaas checkout page,
    so the value is pinned by a test rather than left to a code review.
    """
    from app.config import settings
    from app.models import Member
    from app.plans import PLANS
    from app.services.payments import AsaasProvider

    monkeypatch.setattr(settings, "asaas_api_key", "chave-de-teste")
    recorder: dict = {}
    provider = AsaasProvider()
    monkeypatch.setattr(provider, "_client", lambda: _asaas_client(recorder))

    member = Member(
        id="11111111-2222-3333-4444-555555555555",
        name="Maria da Silva",
        email="maria@exemplo.com.br",
        document="11144477735",
        mobile="91988887777",
        postal_code="66000000",
        login="maria",
        password_hash="x",
    )
    link = provider.create_charge(member, PLANS["PROFISSIONAL"])

    assert recorder["subscription"]["billingType"] == "CREDIT_CARD"
    assert recorder["subscription"]["cycle"] == "MONTHLY"
    assert recorder["subscription"]["value"] == 44.90
    assert recorder["subscription"]["externalReference"] == member.id
    assert link.url == "https://asaas.com/i/abc123"
    assert link.external_reference == "sub_123"
    assert link.provider == "asaas"


def test_asaas_refuses_to_charge_without_an_api_key(monkeypatch):
    from app.config import settings
    from app.models import Member
    from app.plans import PLANS
    from app.services.payments import AsaasProvider, PaymentError

    monkeypatch.setattr(settings, "asaas_api_key", "")
    with pytest.raises(PaymentError):
        AsaasProvider().create_charge(
            Member(id="x", name="N", email="n@e.com", login="n", password_hash="x"),
            PLANS["ESTUDANTE"],
        )


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("PAYMENT_CONFIRMED", "CONFIRMED"),
        ("PAYMENT_RECEIVED", "CONFIRMED"),
        ("PAYMENT_CREDIT_CARD_CAPTURE_REFUSED", "FAILED"),
        ("PAYMENT_OVERDUE", "FAILED"),
        ("PAYMENT_CHARGEBACK_REQUESTED", "REFUNDED"),
        ("PAYMENT_REFUNDED", "REFUNDED"),
        ("PAYMENT_ANYTHING_ELSE", None),
    ],
)
def test_asaas_maps_card_events(event, expected):
    from app.services.payments import AsaasProvider

    reference, status = AsaasProvider().parse_event(
        {"event": event, "payment": {"subscription": "sub_123"}}
    )
    assert reference == "sub_123"
    assert status == expected


def test_only_card_capable_providers_are_registered():
    """A bank-transfer or fixed-link provider would silently reintroduce a
    payment method the association no longer accepts."""
    from app.services.payments import _PROVIDERS

    assert set(_PROVIDERS) == {"mock", "asaas"}


def test_chargeback_cancels_the_membership(client, monkeypatch):
    from sqlalchemy.orm import Session

    from app.config import settings
    from app.models import Member, MemberStatus, Payment, PaymentStatus

    client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    with Session(engine) as db:
        payment = db.query(Payment).first()
        reference = payment.external_reference

    monkeypatch.setattr(settings, "payment_provider", "asaas")
    monkeypatch.setattr(settings, "asaas_webhook_token", "segredo")
    response = client.post(
        "/webhooks/pagamento",
        json={
            "event": "PAYMENT_CHARGEBACK_REQUESTED",
            "payment": {"subscription": reference},
        },
        headers={"asaas-access-token": "segredo"},
    )
    assert response.status_code == 200

    with Session(engine) as db:
        assert db.query(Payment).first().status == PaymentStatus.REFUNDED
        assert db.query(Member).first().status == MemberStatus.CANCELLED


# ==========================================================================
#  E-mail and page copy
# ==========================================================================


def test_welcome_email_carries_the_checkout_link():
    """Regression: Jinja renders an unknown variable as an empty string, so a
    renamed context key turns the payment button into a dead link with no
    error anywhere."""
    from app.plans import PLANS
    from app.services.email import _render

    html = _render(
        "welcome",
        {
            "member": _FakeMember(),
            "plan": PLANS["PROFISSIONAL"],
            "checkout_url": "https://asaas.com/i/abc123",
        },
    )
    assert html.count("https://asaas.com/i/abc123") >= 2
    assert 'href=""' not in html


def test_welcome_email_offers_no_alternative_to_the_card():
    from app.plans import PLANS
    from app.services.email import _render

    html = _render(
        "welcome",
        {
            "member": _FakeMember(),
            "plan": PLANS["PROFISSIONAL"],
            "checkout_url": "https://asaas.com/i/abc123",
        },
    ).lower()
    for method in ("pix", "boleto", "transferência", "agência", "comprovante"):
        assert method not in html


def test_root_sends_visitors_to_the_signup_form(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/associe-se"


def test_the_institutional_site_is_not_part_of_this_deployment(client):
    """Those pages live in a separate project; a stray route here would serve
    a half-built site on the association's real domain."""
    for path in ("/noticias", "/eventos", "/talentos", "/admin"):
        assert client.get(path, follow_redirects=False).status_code == 307


class _FakeMember:
    id = "11111111-2222-3333-4444-555555555555"
    name = "Maria da Silva"
    trade_name = None
    first_name = "Maria"
    email = "maria@exemplo.com.br"
    login = "maria.silva"
    chapter = "Belém"


# ==========================================================================
#  Monthly and annual cycles
# ==========================================================================


def test_annual_plans_are_eleven_months_of_the_monthly_price():
    """Twelve months for the price of eleven. Derived from the monthly plan on
    purpose: a hand-typed annual price drifts the first time 44,90 changes."""
    from app.plans import ANNUAL_MONTHS, MONTHLY_PLANS, PLANS

    for code, monthly in MONTHLY_PLANS.items():
        yearly = PLANS[f"{code}_ANUAL"]
        assert yearly.amount_cents == monthly.amount_cents * ANNUAL_MONTHS
        assert yearly.cycle == "YEARLY"
        assert yearly.audience == monthly.audience
        assert yearly.benefits == monthly.benefits


def test_annual_plan_shows_what_it_saves():
    from app.plans import PLANS

    plan = PLANS["PROFISSIONAL_ANUAL"]
    assert plan.amount_brl == "493,90"
    assert plan.cycle_label == "/ano"
    assert plan.monthly_equivalent_brl == "41,16"
    assert plan.savings_brl == "44,90"  # exactly one month
    assert plan.charge_noun == "anuidade"
    assert PLANS["PROFISSIONAL"].charge_noun == "mensalidade"


def test_signup_page_offers_both_cycles(client):
    """Both cycles ship in the HTML; the toggle only hides one. A visitor with
    JavaScript off must still be able to pick the annual plan."""
    page = client.get("/associe-se").text
    for code in ("ESTUDANTE", "ESTUDANTE_ANUAL", "PROFISSIONAL_ANUAL"):
        assert f'value="{code}"' in page
    assert 'data-cycle="YEARLY"' in page
    assert "493,90" in page

    company = client.get("/associe-se?tipo=pj").text
    assert 'value="EMPRESA_ANUAL"' in company


def test_annual_signup_is_stored_and_charged_as_yearly(client):
    from sqlalchemy.orm import Session

    from app.models import Payment

    response = client.post(
        "/associe-se/pf",
        data={**INDIVIDUAL, "plan_code": "PROFISSIONAL_ANUAL"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as db:
        payment = db.query(Payment).first()
        assert payment.cycle == "YEARLY"
        assert payment.amount_cents == 49390


def test_asaas_sends_a_yearly_subscription_for_the_annual_plan(monkeypatch):
    from app.config import settings
    from app.models import Member
    from app.plans import PLANS
    from app.services.payments import AsaasProvider

    monkeypatch.setattr(settings, "asaas_api_key", "chave-de-teste")
    recorder: dict = {}
    provider = AsaasProvider()
    monkeypatch.setattr(provider, "_client", lambda: _asaas_client(recorder))

    provider.create_charge(
        Member(id="y", name="N", email="n@e.com", login="n", password_hash="x"),
        PLANS["EMPRESA_ANUAL"],
    )
    assert recorder["subscription"]["cycle"] == "YEARLY"
    assert recorder["subscription"]["value"] == 768.90
    assert recorder["subscription"]["billingType"] == "CREDIT_CARD"


def test_an_annual_plan_cannot_be_bought_by_the_wrong_audience(client):
    """EMPRESA_ANUAL is a company tier; the individual form must refuse it."""
    response = client.post(
        "/associe-se/pf", data={**INDIVIDUAL, "plan_code": "EMPRESA_ANUAL"}
    )
    assert response.status_code == 422
    assert "Tipo de associado inválido" in response.text


def test_welcome_email_says_anuidade_for_an_annual_plan():
    from app.plans import PLANS
    from app.services.email import _render

    html = _render(
        "welcome",
        {
            "member": _FakeMember(),
            "plan": PLANS["PROFISSIONAL_ANUAL"],
            "checkout_url": "https://asaas.com/i/abc123",
        },
    )
    assert "anuidade" in html
    assert "mensalidade" not in html
    assert "R$ 493,90/ano" in html


# ==========================================================================
#  Deployment configuration
# ==========================================================================


@pytest.mark.parametrize(
    "given",
    ["postgres://u:p@host/db", "postgresql://u:p@host/db"],
)
def test_a_bare_postgres_url_is_pointed_at_the_installed_driver(given):
    """Hosted providers hand out `postgres://`. SQLAlchemy would read that as
    "use psycopg2", which this project does not install, and crash at boot on
    a URL that was copied correctly."""
    from app.config import Settings

    settings = Settings(database_url=given)
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.database_url.endswith("u:p@host/db")
    assert settings.is_postgres


def test_a_url_that_already_names_a_driver_is_left_alone():
    from app.config import Settings

    for url in ("postgresql+psycopg://u:p@h/db", "sqlite+pysqlite:///./ccpa.db"):
        assert Settings(database_url=url).database_url == url
    assert not Settings(database_url="sqlite+pysqlite:///./ccpa.db").is_postgres


def test_postgres_engines_do_not_hold_a_connection_pool():
    """Serverless instances are frozen without warning, and a pool held by a
    frozen instance still occupies a slot on the database server."""
    from sqlalchemy.pool import NullPool

    from app.database import _engine_options

    assert _engine_options()["connect_args"] == {"check_same_thread": False}

    from app.config import settings

    original = settings.database_url
    try:
        settings.database_url = "postgresql+psycopg://u:p@h/db"
        assert _engine_options()["poolclass"] is NullPool
    finally:
        settings.database_url = original


def test_the_vercel_entrypoint_exposes_the_app():
    """Vercel loads `app` from app/main.py. A rename here deploys a 404 over
    the whole site, and nothing local would notice."""
    import json
    from pathlib import Path

    from fastapi import FastAPI

    from app.main import app as vercel_app

    assert isinstance(vercel_app, FastAPI)

    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "vercel.json").read_text(encoding="utf-8"))
    assert "app/main.py" in config["functions"]

    # Sem o entrypoint declarado como MÓDULO, a Vercel carrega app/main.py como
    # arquivo solto e todo `from .config import ...` estoura com "attempted
    # relative import with no known parent package" — build verde, 500 em toda
    # requisição, e nada local reproduz isso.
    import tomllib

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["vercel"]["entrypoint"] == "app.main:app"

    excluded = config["functions"]["app/main.py"]["excludeFiles"]
    # The stylesheet, the scripts and the logo must stay in the bundle: the
    # app serves them itself, and excluding them ships an unstyled page.
    assert "app/static" not in excluded
    assert "app/templates" not in excluded


def test_the_webhook_script_subscribes_to_every_event_it_handles():
    """Registering the webhook by hand in the Asaas dashboard means ticking
    boxes, and the card-specific ones are the easy ones to miss: a chargeback
    nobody is told about leaves a cancelled member looking active."""
    import importlib.util
    from pathlib import Path

    from app.services.payments import AsaasProvider

    caminho = Path(__file__).resolve().parents[1] / "scripts" / "setup_webhook.py"
    spec = importlib.util.spec_from_file_location("setup_webhook", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)

    assert set(modulo.EVENTS) == set(AsaasProvider.STATUS_MAP) | set(
        AsaasProvider.AUTHORIZATION_EVENTS
    )
    assert "PIX_AUTOMATIC_RECURRING_AUTHORIZATION_CANCELLED" in modulo.EVENTS
    assert "PAYMENT_CHARGEBACK_REQUESTED" in modulo.EVENTS
    assert "PAYMENT_CREDIT_CARD_CAPTURE_REFUSED" in modulo.EVENTS
    assert modulo.WEBHOOK_PATH == "/webhooks/pagamento"


def test_a_generated_token_is_accepted_by_the_webhook(client, monkeypatch):
    """The token the script prints has to satisfy both sides: Asaas requires
    32 characters and no whitespace, and our own check has to accept it."""
    import secrets

    from sqlalchemy.orm import Session

    from app.config import settings
    from app.models import Member, MemberStatus, Payment

    token = secrets.token_urlsafe(32)
    assert 32 <= len(token) <= 255
    assert not any(c.isspace() for c in token)

    client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    with Session(engine) as db:
        reference = db.query(Payment).first().external_reference

    monkeypatch.setattr(settings, "payment_provider", "asaas")
    monkeypatch.setattr(settings, "asaas_webhook_token", token)

    response = client.post(
        "/webhooks/pagamento",
        json={"event": "PAYMENT_CONFIRMED", "payment": {"subscription": reference}},
        headers={"asaas-access-token": token},
    )
    assert response.status_code == 200

    with Session(engine) as db:
        assert db.query(Member).first().status == MemberStatus.ACTIVE


def test_the_two_dependency_lists_say_the_same_thing():
    """`pyproject.toml` and `requirements.txt` both list the dependencies, and
    they must agree.

    Vercel reads `pyproject.toml` first: a `[project]` table without a
    `dependencies` key is read as "no dependencies at all", so it installs
    nothing and every request dies on `No module named 'fastapi'` — with a
    green build and no hint in the build log. `requirements.txt` stays because
    it is what `pip install -r` uses locally. This test is what keeps the two
    from drifting.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    linhas = [
        linha.split("#")[0].strip()
        for linha in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
    ]
    do_requirements = {linha for linha in linhas if linha}

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    do_pyproject = {dep.strip() for dep in pyproject["project"]["dependencies"]}

    # Normalises the spacing around the environment marker, which each file
    # writes in its own style.
    def limpar(conjunto):
        return {" ".join(item.replace(" ;", ";").split()) for item in conjunto}

    assert limpar(do_requirements) == limpar(do_pyproject)
    assert any(d.startswith("fastapi") for d in do_pyproject)


# ==========================================================================
#  Running without e-mail
# ==========================================================================


def test_without_email_the_page_does_not_promise_one(client, monkeypatch):
    """Telling someone to check an inbox that will never receive anything is
    how a signup is lost: they close the tab and wait."""
    from app.config import settings

    monkeypatch.setattr(settings, "resend_api_key", "")
    assert not settings.email_enabled

    response = client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    page = client.get(response.headers["location"]).text

    assert "Enviamos um e-mail" not in page
    assert "Confira seu e-mail" not in page
    assert "guarde o endereço desta página" in page.lower()


def test_without_email_the_page_carries_the_way_back(client, monkeypatch):
    """The confirmation page is the only delivery of the payment link, so it
    has to show the address that returns to it, as copyable text."""
    from sqlalchemy.orm import Session

    from app.config import settings
    from app.models import Member

    monkeypatch.setattr(settings, "resend_api_key", "")

    response = client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    page = client.get(response.headers["location"]).text

    with Session(engine) as db:
        member_id = db.query(Member).first().id

    assert f"/associe-se/sucesso/{member_id}" in page
    assert "/pagamento/simulado/" in page  # o botão de pagamento continua lá


def test_with_email_the_page_keeps_pointing_at_the_inbox(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "resend_api_key", "re_chave_de_teste")
    assert settings.email_enabled

    response = client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    page = client.get(response.headers["location"]).text

    assert "Enviamos um e-mail" in page
    assert "Guarde este endereço" not in page


# ------------------- every refusal says what is wrong ---------------------
# A refused signup that shows only "check the highlighted fields" with nothing
# highlighted is a visitor who gives up. It happened with a CPF that was
# already registered: the error was keyed to "document", a name no input on
# the page has.
def test_duplicate_cpf_is_explained_next_to_the_cpf_field(client):
    first = client.post("/associe-se/pf", data=INDIVIDUAL, follow_redirects=False)
    assert first.status_code == 303
    again = {**INDIVIDUAL, "email": "outra@exemplo.com.br", "login": "outro.login"}
    response = client.post("/associe-se/pf", data=again)
    assert response.status_code == 409
    assert "Este CPF já tem um cadastro" in response.text
    assert 'id="e-cpf_profissional"' in response.text  # under the CPF input
    # ...of the tab the form was sent from, which is the one open again.
    assert (
        'id="tab-profissional" aria-controls="painel-profissional"\n              aria-selected="true"'
        in response.text
    )


def test_duplicate_cnpj_is_explained_next_to_the_cnpj_field(client):
    first = client.post("/associe-se/pj", data=COMPANY, follow_redirects=False)
    assert first.status_code == 303
    again = {**COMPANY, "email": "outra@agencia.com.br", "login": "outra.agencia"}
    response = client.post("/associe-se/pj", data=again)
    assert response.status_code == 409
    assert "Este CNPJ já tem um cadastro" in response.text
    assert 'id="e-cnpj_pj"' in response.text


def test_password_mismatch_is_shown_in_the_form_box(client):
    data = {**INDIVIDUAL, "password_confirmation": "outra1234"}
    response = client.post("/associe-se/pf", data=data)
    assert response.status_code == 422
    assert "As senhas não conferem" in response.text


def test_errors_without_an_input_move_to_the_form_box():
    from app.routers.membership import _visible_errors

    form = {"cpf": "1", "email": "x"}
    shown = _visible_errors({"document": "Duplicado.", "email": "Inválido."}, form)
    assert shown == {"email": "Inválido.", "_general": "Duplicado."}


def test_every_page_says_it_is_not_the_official_site(client):
    """Public portfolio copy: the club authorised it on the condition that it
    is clearly marked as an inspiration, not the official site."""
    page = client.get("/associe-se")
    assert "não é o site oficial" in page.text
    assert 'content="noindex, nofollow"' in page.text
    login = client.get("/painel/entrar")
    assert "não é o site oficial" in login.text

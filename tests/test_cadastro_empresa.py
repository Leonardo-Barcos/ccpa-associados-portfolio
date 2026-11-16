"""Company (CNPJ) signup, end to end — and every way it can be refused.

Written after a company could not get past the form: each refusal here must
reach the visitor as a sentence next to the right field, never as a bare
"check the highlighted fields" with nothing highlighted.
"""

import os
import re
import tempfile

import httpx
import pytest

os.environ.setdefault("APP_ENV", "development")
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{tempfile.gettempdir()}/ccpa_test.db"
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["RESEND_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Member  # noqa: E402
from app.schemas import CompanySignup, IndividualSignup  # noqa: E402
from app.validators import is_valid_cnpj, normalize_cnpj  # noqa: E402

COMPANY = {
    "plan_code": "EMPRESA",
    "legal_name": "Agência Criativa Ltda",
    "trade_name": "Agência Criativa",
    "cnpj": "11.222.333/0001-81",
    "chapter": "Canaã dos Carajás",
    "mobile": "(94) 99111-2233",
    "email": "contato@agencia.com.br",
    "postal_code": "68537-000",
    "state": "PA",
    "city": "Canaã dos Carajás",
    "login": "agencia.criativa",
    "password": "senha1234",
    "password_confirmation": "senha1234",
    "accepted_terms": "on",
}

# The Receita Federal's own example of the alphanumeric format.
ALPHANUMERIC_CNPJ = "12.ABC.345/01DE-35"


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


def members() -> int:
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(Member))


def input_names(html: str, action: str) -> set[str]:
    form = html.split(f'action="{action}"', 1)[1].split("</form>", 1)[0]
    return set(re.findall(r'name="([a-z_]+)"', form))


# ------------------------------- the form ---------------------------------
@pytest.mark.parametrize(
    "schema,action",
    [(CompanySignup, "/associe-se/pj"), (IndividualSignup, "/associe-se/pf")],
)
def test_every_field_the_server_checks_has_an_input_on_the_page(client, schema, action):
    """An error on a field the page does not render is an error nobody sees."""
    names = input_names(client.get("/associe-se").text, action)
    missing = set(schema.model_fields) - names
    assert not missing, f"{action} has no input for {sorted(missing)}"


def test_cnpj_input_accepts_letters(client):
    page = client.get("/associe-se?tipo=pj").text
    cnpj_input = re.search(r'<input[^>]*name="cnpj"[^>]*>', page).group(0)
    assert 'inputmode="numeric"' not in cnpj_input  # would hide the letters


# ------------------------------ happy paths -------------------------------
def test_company_signup_goes_to_payment(client):
    response = client.post("/associe-se/pj", data=COMPANY)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/associe-se/sucesso/")
    with Session(engine) as db:
        member = db.scalars(select(Member)).one()
        assert member.document == "11222333000181"
        assert member.chapter == "Canaã dos Carajás"


def test_alphanumeric_cnpj_is_accepted_and_stored_normalised(client):
    data = {**COMPANY, "cnpj": ALPHANUMERIC_CNPJ.lower()}
    assert client.post("/associe-se/pj", data=data).status_code == 303
    with Session(engine) as db:
        assert db.scalars(select(Member)).one().document == "12ABC34501DE35"


def test_cnpj_validator_follows_the_receita_rules():
    assert is_valid_cnpj("11.222.333/0001-81")  # classic, all digits
    assert is_valid_cnpj(ALPHANUMERIC_CNPJ)  # new format
    assert not is_valid_cnpj("12.ABC.345/01DE-36")  # wrong check digit
    assert not is_valid_cnpj("12.ABC.345/01DE-3A")  # check digits are numeric
    assert not is_valid_cnpj("00.000.000/0000-00")
    assert normalize_cnpj(" 12.abc.345/01de-35 ") == "12ABC34501DE35"


# ------------------------------ refusals ----------------------------------
@pytest.mark.parametrize(
    "field,value,message",
    [
        ("cnpj", "11.222.333/0001-00", "CNPJ inválido"),
        ("cnpj", "", "Campo obrigatório"),
        ("legal_name", "", "Campo obrigatório"),
        ("trade_name", "", "Campo obrigatório"),
        ("chapter", "", "Campo obrigatório"),
        ("email", "nao-e-email", "E-mail inválido"),
        ("mobile", "123", "Telefone inválido"),
        ("postal_code", "123", "CEP inválido"),
        ("login", "ab", "Valor muito curto"),
        ("password", "curta1", "Valor muito curto"),
    ],
)
def test_each_refusal_names_the_problem(client, field, value, message):
    response = client.post("/associe-se/pj", data={**COMPANY, field: value})
    assert response.status_code == 422
    assert message in response.text
    assert members() == 0


def test_password_mismatch_and_terms_are_explained(client):
    mismatch = {**COMPANY, "password_confirmation": "outra1234"}
    assert "As senhas não conferem" in client.post("/associe-se/pj", data=mismatch).text
    no_terms = {k: v for k, v in COMPANY.items() if k != "accepted_terms"}
    response = client.post("/associe-se/pj", data=no_terms)
    assert "aceitar os termos" in response.text


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"email": "outro@agencia.com.br", "login": "outra.agencia"}, "Este CNPJ"),
        ({"cnpj": "11.444.777/0001-61", "login": "outra.agencia"}, "e-mail"),
        ({"cnpj": "11.444.777/0001-61", "email": "b@agencia.com.br"}, "login"),
    ],
)
def test_duplicates_are_explained(client, changes, message):
    assert client.post("/associe-se/pj", data=COMPANY).status_code == 303
    response = client.post("/associe-se/pj", data={**COMPANY, **changes})
    assert response.status_code == 409
    assert message in response.text
    assert "Confira os campos destacados" not in response.text


# ---------------------------- the gateway ---------------------------------
def fake_asaas(handler):
    def factory(self):  # noqa: ARG001 — replaces AsaasProvider._client
        return httpx.Client(
            base_url="https://api.asaas.test/v3",
            transport=httpx.MockTransport(handler),
        )

    return factory


@pytest.fixture
def asaas(monkeypatch):
    from app.config import settings
    from app.services.payments import AsaasProvider

    monkeypatch.setattr(settings, "payment_provider", "asaas")
    monkeypatch.setattr(settings, "asaas_api_key", "test-key")

    def install(handler):
        monkeypatch.setattr(AsaasProvider, "_client", fake_asaas(handler))

    return install


def test_asaas_refusal_is_shown_and_nothing_is_saved(client, asaas):
    asaas(
        lambda request: httpx.Response(
            400,
            json={
                "errors": [
                    {
                        "code": "invalid_cpfCnpj",
                        "description": "O CPF/CNPJ informado é inválido.",
                    }
                ]
            },
        )
    )
    response = client.post("/associe-se/pj", data=COMPANY)
    assert response.status_code == 502
    assert "O CPF/CNPJ informado é inválido." in response.text
    assert members() == 0  # the visitor can simply try again


def test_asaas_down_gives_a_calm_message(client, asaas):
    def boom(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    asaas(boom)
    response = client.post("/associe-se/pj", data=COMPANY)
    assert response.status_code == 502
    assert "Não conseguimos gerar a sua cobrança agora" in response.text
    assert members() == 0


def test_asaas_happy_path_sends_the_cnpj_and_returns_the_invoice(client, asaas):
    seen = {}

    def handler(request):
        if request.url.path.endswith("/customers"):
            seen["customer"] = request.content.decode()
            return httpx.Response(200, json={"id": "cus_1"})
        if request.url.path.endswith("/subscriptions"):
            return httpx.Response(200, json={"id": "sub_1"})
        return httpx.Response(
            200, json={"data": [{"invoiceUrl": "https://www.asaas.com/i/1"}]}
        )

    asaas(handler)
    response = client.post("/associe-se/pj", data={**COMPANY, "cnpj": ALPHANUMERIC_CNPJ})
    assert response.status_code == 303
    assert '"cpfCnpj":"12ABC34501DE35"' in seen["customer"].replace(" ", "")
    assert members() == 1


# ------------------------ addresses as people type them -------------------
# A <input type="url"> refuses "www.agencia.com.br" in the browser, before the
# server ever sees it — and the refusal was a bubble that vanished in a
# second. To the person filling the form, the button simply did nothing.
def test_no_input_demands_a_full_url(client):
    assert 'type="url"' not in client.get("/associe-se").text


def test_site_without_https_is_accepted_and_completed(client):
    data = {**COMPANY, "website": "www.agencia.com.br"}
    assert client.post("/associe-se/pj", data=data).status_code == 303
    with Session(engine) as db:
        assert db.scalars(select(Member)).one().website == "https://www.agencia.com.br"


def test_linkedin_without_https_is_accepted_and_completed(client):
    data = {
        "plan_code": "ESTUDANTE",
        "name": "Maria da Silva",
        "mobile": "(91) 98888-7777",
        "email": "maria@exemplo.com.br",
        "linkedin": "linkedin.com/in/maria",
        "login": "maria.silva",
        "password": "senha1234",
        "password_confirmation": "senha1234",
        "accepted_terms": "on",
    }
    assert client.post("/associe-se/pf", data=data).status_code == 303
    with Session(engine) as db:
        member = db.scalars(select(Member)).one()
        assert member.linkedin == "https://linkedin.com/in/maria"


def test_every_submit_button_has_a_place_to_explain_a_block(client):
    page = client.get("/associe-se").text
    assert page.count('class="erro-envio"') == 3  # one per form


def test_three_coloured_tabs(client):
    """A company once signed up as a student because it never noticed the
    second tab. Now there are three, each in a club colour."""
    page = client.get("/associe-se").text
    for key in ("estudante", "profissional", "pj"):
        assert f'class="aba aba--{key}"' in page
        assert f'id="painel-{key}"' in page
    assert "Escolha a aba do seu perfil" in page
    assert "CNPJ</span>" not in page  # no black badge any more


def test_each_individual_tab_has_only_its_own_plan(client):
    page = client.get("/associe-se").text
    student = page[
        page.index('id="painel-estudante"') : page.index('id="painel-profissional"')
    ]
    professional = page[
        page.index('id="painel-profissional"') : page.index('id="painel-pj"')
    ]
    assert 'value="ESTUDANTE"' in student and 'value="PROFISSIONAL"' not in student
    assert (
        'value="PROFISSIONAL"' in professional and 'value="ESTUDANTE"' not in professional
    )


@pytest.mark.parametrize(
    ("tipo", "open_tab"),
    [("estudante", "estudante"), ("profissional", "profissional"), ("pj", "pj"),
     ("pf", "estudante"), ("qualquer", "estudante")],
)  # fmt: skip
def test_tipo_opens_the_right_tab(client, tipo, open_tab):
    page = client.get(f"/associe-se?tipo={tipo}").text
    assert (
        f'id="tab-{open_tab}" aria-controls="painel-{open_tab}"\n              aria-selected="true"'
        in page
    )

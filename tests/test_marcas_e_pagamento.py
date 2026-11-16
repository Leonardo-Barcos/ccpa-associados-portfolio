"""Logos of member companies and changing a member's payment method."""

import io
import json
import os
import tempfile

import httpx
import pytest

os.environ.setdefault("APP_ENV", "development")
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{tempfile.gettempdir()}/ccpa_test.db"
os.environ["PAYMENT_PROVIDER"] = "mock"
os.environ["RESEND_API_KEY"] = ""

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import inspect, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base, engine, ensure_schema  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AdminRole,
    AdminUser,
    AuditLog,
    BrandLogo,
    EntityType,
    Member,
    MemberStatus,
    Payment,
    PaymentStatus,
)
from app.security import hash_password  # noqa: E402
from app.services import marcas  # noqa: E402
from app.services import painel as svc  # noqa: E402
from app.services.payments import AsaasProvider  # noqa: E402

PASSWORD = "senha-forte-123"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def drawing(fmt="PNG", size=(800, 400), box=(300, 150, 500, 250), color=(20, 20, 20)):
    """A logo as designers export it: a small mark in a big empty artboard."""
    from PIL import Image, ImageDraw

    mode = "RGBA" if fmt == "PNG" else "RGB"
    background = (0, 0, 0, 0) if mode == "RGBA" else (255, 255, 255)
    image = Image.new(mode, size, background)
    ImageDraw.Draw(image).rectangle(box, fill=color)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def pdf(*boxes):
    """A PDF with one page per box — several versions of the same logo."""
    from PIL import Image, ImageDraw

    pages = []
    for box in boxes:
        page = Image.new("RGB", (500, 500), (255, 255, 255))
        ImageDraw.Draw(page).rectangle(box, fill=(30, 30, 30))
        pages.append(page)
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    return buffer.getvalue()


PNG = drawing()


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


def make_user(email="leo@ccpa.org", role=AdminRole.ADMIN):
    with Session(engine) as db:
        db.add(
            AdminUser(
                name="Leonardo",
                email=email,
                role=role,
                password_hash=hash_password(PASSWORD),
                must_change_password=False,
            )
        )
        db.commit()


def csrf(client) -> str:
    client.get("/painel/entrar")
    return client.cookies.get("ccpa_painel_csrf")


def login(client, email="leo@ccpa.org"):
    make_user(
        email=email, role=AdminRole.ADMIN if email == "leo@ccpa.org" else AdminRole.EQUIPE
    )
    client.post(
        "/painel/entrar",
        data={"email": email, "password": PASSWORD, "csrf": csrf(client)},
    )


def the(model):
    with Session(engine) as db:
        return db.scalars(select(model)).all()


def upload(client, name="Agência Açaí", data=PNG, site="www.acai.com.br"):
    return client.post(
        "/painel/marcas",
        data={"nome": name, "site": site, "csrf": client.cookies.get("ccpa_painel_csrf")},
        files={"arquivo": ("logo.png", data, "image/png")},
    )


# ============================== LOGOS ======================================
def test_ensure_schema_creates_a_missing_table():
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE brand_logos"))
    assert "brand_logos" not in inspect(engine).get_table_names()
    added = ensure_schema()
    assert "brand_logos" in added
    assert "brand_logos" in inspect(engine).get_table_names()


def test_signup_page_has_no_strip_without_logos(client):
    page = client.get("/associe-se")
    assert page.status_code == 200
    assert "Empresas associadas" not in page.text


def test_logos_page_is_admin_only(client):
    assert client.get("/painel/marcas").status_code == 303  # not signed in
    login(client, email="equipe@ccpa.org")
    assert client.get("/painel/marcas").status_code == 403
    response = upload(client)
    assert response.status_code == 403
    assert the(BrandLogo) == []


def test_admin_uploads_a_logo_and_it_shows_on_the_signup_page(client):
    login(client)
    response = upload(client)
    assert response.status_code == 303
    [logo] = the(BrandLogo)
    assert logo.name == "Agência Açaí"
    assert logo.website == "https://www.acai.com.br"
    assert logo.content_type == "image/png"

    page = client.get("/associe-se")
    assert "Empresas associadas" in page.text
    assert f'src="/marcas/{logo.id}?v=' in page.text
    assert 'alt="Agência Açaí"' in page.text
    # One logo: standing still, not a marquee.
    assert "parceiros--rolando" not in page.text

    image = client.get(f"/marcas/{logo.id}?v=1")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert "immutable" in image.headers["cache-control"]

    [entry] = [a for a in the(AuditLog) if a.action == "adicionou_marca"]
    assert "Agência Açaí" in entry.detail


def size_of(png: bytes) -> tuple[int, int]:
    from PIL import Image

    return Image.open(io.BytesIO(png)).size


def test_empty_margins_are_cut_off():
    # 800x400 artboard, the mark is 201x101 in the middle.
    [png] = marcas.prepare(drawing())
    assert size_of(png) == (201, 101)
    # Same on a white (not transparent) JPEG background.
    [png] = marcas.prepare(drawing("JPEG"))
    width, height = size_of(png)
    # JPEG blurs the edges a little, so a few pixels of slack.
    assert 195 <= width <= 216 and 95 <= height <= 116


def test_big_images_are_scaled_down():
    [png] = marcas.prepare(drawing(size=(4000, 2000), box=(0, 0, 3999, 1999)))
    width, height = size_of(png)
    assert width <= marcas.MAX_WIDTH and height <= marcas.MAX_HEIGHT


def pixel(png: bytes, xy):
    from PIL import Image

    return Image.open(io.BytesIO(png)).convert("RGBA").getpixel(xy)


def test_white_version_keeps_the_drawing_inside_the_logo():
    """Black square with a white hole: the square turns white, the hole
    turns transparent — not a solid white block."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 99, 99), fill=(20, 20, 20, 255))
    draw.rectangle((30, 30, 69, 69), fill=(255, 255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    white = marcas.whiten(buffer.getvalue())
    assert pixel(white, (5, 5)) == (255, 255, 255, 255)
    assert pixel(white, (50, 50))[3] == 0


def test_a_logo_that_is_already_white_does_not_vanish():
    [png] = marcas.prepare(drawing(color=(255, 255, 255)))
    white = marcas.whiten(png)
    assert pixel(white, (10, 10)) == (255, 255, 255, 255)


def test_logo_is_served_white_unless_marked_original(client):
    login(client)
    upload(client)
    [logo] = the(BrandLogo)
    assert logo.tone == "branca"
    served = client.get(logo.url).content
    assert pixel(served, (10, 10)) == (255, 255, 255, 255)

    client.post(
        f"/painel/marcas/{logo.id}/cor",
        data={"csrf": client.cookies.get("ccpa_painel_csrf")},
    )
    [logo] = the(BrandLogo)
    assert logo.tone == "original"
    served = client.get(logo.url).content
    assert pixel(served, (10, 10))[:3] == (20, 20, 20)
    assert "parceiros__item--cartao" in client.get("/associe-se").text
    assert "mudou_cor_marca" in {a.action for a in the(AuditLog)}


def test_logos_sit_beside_the_headline(client):
    login(client)
    upload(client)
    page = client.get("/associe-se").text
    hero = page[page.index('<section class="hero">') : page.index("</section>")]
    assert "hero__grade--parceiros" in hero
    assert '<aside class="parceiros parceiros--uma"' in hero


def test_jpeg_and_webp_are_accepted():
    for fmt in ("JPEG", "WEBP"):
        [png] = marcas.prepare(drawing(fmt))
        assert marcas.sniff(png) == "image/png"  # always stored as PNG


def test_single_page_pdf_goes_straight_in(client):
    login(client)
    response = upload(client, data=pdf((100, 200, 400, 300)))
    assert response.status_code == 303
    [logo] = the(BrandLogo)
    assert logo.content_type == "image/png"
    assert client.get(f"/marcas/{logo.id}").content.startswith(b"\x89PNG")


def test_pdf_with_several_versions_asks_which_one(client):
    login(client)
    response = upload(client, data=pdf((100, 200, 400, 300), (200, 100, 300, 400)))
    assert response.status_code == 200
    assert "Qual versão da logo?" in response.text
    assert response.text.count('name="escolha"') == 2
    assert the(BrandLogo) == []

    # The admin picks the second (tall) version.
    import re

    choices = re.findall(r'name="escolha" value="([^"]+)"', response.text)
    saved = client.post(
        "/painel/marcas/escolher",
        data={
            "csrf": client.cookies.get("ccpa_painel_csrf"),
            "escolha": choices[1],
            "nome": "Agência Açaí",
            "site": "",
            "associado": "",
        },
    )
    assert saved.status_code == 303
    [logo] = the(BrandLogo)
    width, height = size_of(client.get(f"/marcas/{logo.id}").content)
    assert height > width


def test_a_forged_choice_is_refused(client):
    login(client)
    response = client.post(
        "/painel/marcas/escolher",
        data={
            "csrf": client.cookies.get("ccpa_painel_csrf"),
            "escolha": "data:image/svg+xml;base64,"
            + __import__("base64").b64encode(SVG).decode(),
            "nome": "X Ltda",
        },
    )
    assert response.status_code == 422
    assert the(BrandLogo) == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (SVG, "Formato não aceito"),
        (b"GIF89a" + b"\x00" * 20, "Formato não aceito"),
        (b"\x89PNG\r\n\x1a\n" + b"lixo" * 10, "Não foi possível ler"),
        (b"%PDF-1.4 lixo", "Não foi possível abrir o PDF"),
        # The oversized file is built inside the test: as a parameter it
        # becomes part of the test's name, which pytest puts in an
        # environment variable — and Windows refuses variables over 32 767
        # characters.
        ("TOO_BIG", "grande demais"),
        (b"", "Escolha o arquivo"),
    ],
    ids=["svg", "gif", "png-quebrado", "pdf-quebrado", "grande-demais", "vazio"],
)
def test_bad_uploads_are_refused(client, data, message):
    if data == "TOO_BIG":
        data = PNG + b"\x00" * marcas.MAX_BYTES
    login(client)
    response = upload(client, data=data)
    assert response.status_code == 422
    assert message in response.text
    assert the(BrandLogo) == []


def test_marquee_starts_at_the_threshold(client):
    login(client)
    for index in range(marcas.MARQUEE_MIN):
        upload(client, name=f"Empresa {index}", site="")
    page = client.get("/associe-se").text
    assert "parceiros--rolando" in page
    # Every logo readable once; the repeated copies are hidden from screen readers.
    for index in range(marcas.MARQUEE_MIN):
        assert page.count(f'alt="Empresa {index}"') == 1
    assert 'aria-hidden="true"' in page


def test_marquee_repeats_short_lists_to_fill_the_track():
    assert marcas.marquee_copies(0) == 0
    assert marcas.marquee_copies(1) == 8
    assert marcas.marquee_copies(4) == 2
    assert marcas.marquee_copies(12) == 1


def test_hide_reorder_and_remove(client):
    login(client)
    upload(client, name="Primeira")
    upload(client, name="Segunda")
    first, second = sorted(the(BrandLogo), key=lambda logo: logo.position)
    token = client.cookies.get("ccpa_painel_csrf")

    client.post(f"/painel/marcas/{second.id}/subir", data={"csrf": token})
    assert [logo.name for logo in marcas_ordered()] == ["Segunda", "Primeira"]

    client.post(f"/painel/marcas/{first.id}/ativo", data={"csrf": token})
    page = client.get("/associe-se").text
    assert 'alt="Primeira"' not in page
    assert 'alt="Segunda"' in page
    assert client.get(f"/marcas/{first.id}").status_code == 404
    # The panel still shows the hidden one.
    assert client.get(f"/painel/marcas/{first.id}/imagem").status_code == 200

    client.post(f"/painel/marcas/{second.id}/remover", data={"csrf": token})
    assert [logo.name for logo in the(BrandLogo)] == ["Primeira"]
    actions = {a.action for a in the(AuditLog)}
    assert {"reordenou_marcas", "ocultou_marca", "removeu_marca"} <= actions


def test_logo_actions_need_the_csrf_token(client):
    login(client)
    upload(client)
    [logo] = the(BrandLogo)
    response = client.post(f"/painel/marcas/{logo.id}/remover", data={})
    assert response.status_code == 403
    assert len(the(BrandLogo)) == 1


def test_logo_from_the_member_record_needs_only_the_file(client):
    member_id = make_member()
    with Session(engine) as db:
        db.get(Member, member_id).website = "https://tucupi.com.br"
        db.commit()
    login(client)
    ficha = client.get(f"/painel/associados/{member_id}").text
    assert "Logo na página de cadastro" in ficha

    response = client.post(
        "/painel/marcas",
        data={
            "csrf": client.cookies.get("ccpa_painel_csrf"),
            "associado": member_id,
            "voltar": "ficha",
        },
        files={"arquivo": ("brado.pdf", pdf((100, 200, 400, 300)), "application/pdf")},
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith(
        f"/painel/associados/{member_id}?logo="
    )
    [logo] = the(BrandLogo)
    assert (logo.name, logo.website, logo.member_id) == (
        "Estúdio Tucupi",
        "https://tucupi.com.br",
        member_id,
    )
    ficha = client.get(response.headers["location"]).text
    assert "Aparecendo na faixa" in ficha
    assert "Logo de Estúdio Tucupi adicionada." in ficha


def test_logo_error_from_the_record_returns_to_the_record(client):
    member_id = make_member()
    login(client)
    response = client.post(
        "/painel/marcas",
        data={
            "csrf": client.cookies.get("ccpa_painel_csrf"),
            "associado": member_id,
            "voltar": "ficha",
        },
        files={"arquivo": ("x.svg", SVG, "image/svg+xml")},
    )
    assert response.status_code == 303
    assert "logo_erro=" in response.headers["location"]
    assert "Formato não aceito" in client.get(response.headers["location"]).text


def test_picking_the_member_in_the_logos_page_fills_name_and_site(client):
    member_id = make_member()
    login(client)
    page = client.get("/painel/marcas").text
    assert f'value="{member_id}"' in page  # company members are listed
    client.post(
        "/painel/marcas",
        data={"csrf": client.cookies.get("ccpa_painel_csrf"), "associado": member_id},
        files={"arquivo": ("logo.png", PNG, "image/png")},
    )
    [logo] = the(BrandLogo)
    assert logo.name == "Estúdio Tucupi"
    assert "Associado:" in client.get("/painel/marcas").text


def test_deleting_the_member_keeps_the_logo_unlinked(client):
    member_id = make_member()
    login(client)
    client.post(
        "/painel/marcas",
        data={"csrf": client.cookies.get("ccpa_painel_csrf"), "associado": member_id},
        files={"arquivo": ("logo.png", PNG, "image/png")},
    )
    client.post(
        f"/painel/associados/{member_id}/excluir",
        data={
            "csrf": client.cookies.get("ccpa_painel_csrf"),
            "motivo": "teste",
            "confirmacao": "EXCLUIR",
        },
    )
    assert the(Member) == []
    [logo] = the(BrandLogo)
    assert logo.member_id is None


def marcas_ordered():
    with Session(engine) as db:
        return marcas.ordered(db)


# ======================== PAYMENT METHOD CHANGE ============================
def make_member(
    status=MemberStatus.AWAITING_PAYMENT,
    method="CREDIT_CARD",
    provider="mock",
    reference="mock_1",
    paid=False,
) -> str:
    with Session(engine) as db:
        member = Member(
            entity_type=EntityType.PJ,
            plan_code="EMPRESA",
            status=status,
            name="Estúdio Tucupi Ltda",
            trade_name="Estúdio Tucupi",
            document="11222333000181",
            email="contato@tucupi.com.br",
            mobile="91988887777",
            login="tucupi",
            password_hash="x",
            accepted_terms=True,
        )
        db.add(member)
        db.flush()
        db.add(
            Payment(
                member_id=member.id,
                provider=provider,
                external_reference=reference,
                amount_cents=19990,
                cycle="MONTHLY",
                method=method,
                provider_customer_id="cus_1",
                status=PaymentStatus.CONFIRMED if paid else PaymentStatus.PENDING,
                checkout_url="https://www.asaas.com/i/old",
            )
        )
        db.commit()
        return member.id


def change(client, member_id, method, reason="prefere pagar no Pix"):
    return client.post(
        f"/painel/associados/{member_id}/pagamento",
        data={
            "metodo": method,
            "motivo": reason,
            "csrf": client.cookies.get("ccpa_painel_csrf"),
        },
    )


def payment_of(member_id) -> Payment:
    with Session(engine) as db:
        return db.scalars(select(Payment).where(Payment.member_id == member_id)).one()


def test_change_page_is_admin_only(client):
    member_id = make_member()
    login(client, email="equipe@ccpa.org")
    assert client.get(f"/painel/associados/{member_id}/pagamento").status_code == 403
    assert change(client, member_id, "PIX").status_code == 403
    assert payment_of(member_id).method == "CREDIT_CARD"


def test_card_to_pix_keeps_the_registration(client):
    member_id = make_member()
    login(client)
    page = client.get(f"/painel/associados/{member_id}/pagamento")
    assert page.status_code == 200
    assert "Pix (cobrança a cada período)" in page.text

    response = change(client, member_id, "PIX")
    assert response.status_code == 303
    assert response.headers["location"] == f"/painel/associados/{member_id}?pagamento=ok"
    payment = payment_of(member_id)
    assert payment.method == "PIX"
    assert payment.is_pix_invoice
    assert len(the(Member)) == 1  # same record, nobody signs up again

    ficha = client.get(response.headers["location"]).text
    assert "Forma de pagamento alterada" in ficha
    [entry] = [a for a in the(AuditLog) if a.action == "trocou_pagamento"]
    assert "Cartão de crédito → Pix" in entry.detail
    assert "prefere pagar no Pix" in entry.detail


def test_reason_is_required_and_same_method_refused(client):
    member_id = make_member()
    login(client)
    assert change(client, member_id, "PIX", reason="").status_code == 422
    assert change(client, member_id, "CREDIT_CARD").status_code == 422
    assert change(client, member_id, "BOLETO").status_code == 422
    assert payment_of(member_id).method == "CREDIT_CARD"


def test_success_page_and_whatsapp_follow_the_new_method(client):
    member_id = make_member()
    login(client)
    change(client, member_id, "PIX")
    page = client.get(f"/associe-se/sucesso/{member_id}").text
    assert "Pagar com Pix" in page
    assert "Cadastrar meu cartão" not in page

    with Session(engine) as db:
        member = db.get(Member, member_id)
        text_ = svc.whatsapp_message(member, svc.latest_payment(db, member), "cobranca")
    assert "pagar com Pix" in text_
    assert "cartão" not in text_


def test_export_shows_the_new_method(client):
    member_id = make_member()
    login(client)
    change(client, member_id, "PIX")
    csv = client.get("/painel/associados/exportar.csv").content.decode("utf-8")
    assert "Pix (cobrança a cada período)" in csv


def test_pix_automatic_is_not_offered_to_a_member_who_already_paid(client, monkeypatch):
    monkeypatch.setattr(settings, "pix_automatic_enabled", True)
    member_id = make_member(status=MemberStatus.ACTIVE, paid=True)
    login(client)
    response = change(client, member_id, "PIX_AUTOMATIC")
    assert response.status_code == 409
    assert "cobraria de novo" in response.text
    assert payment_of(member_id).method == "CREDIT_CARD"
    # Card -> plain Pix is still fine for a paying member.
    assert change(client, member_id, "PIX").status_code == 303


def test_pix_automatic_is_shown_off_while_disabled(client):
    member_id = make_member()
    login(client)
    page = client.get(f"/painel/associados/{member_id}/pagamento").text
    assert "Pix Automático" in page
    assert "falta a Asaas liberar" in page
    response = change(client, member_id, "PIX_AUTOMATIC")
    assert response.status_code == 409
    assert payment_of(member_id).method == "CREDIT_CARD"


def test_unpaid_member_can_move_to_pix_automatic(client, monkeypatch):
    monkeypatch.setattr(settings, "pix_automatic_enabled", True)
    member_id = make_member()
    login(client)
    assert change(client, member_id, "PIX_AUTOMATIC").status_code == 303
    payment = payment_of(member_id)
    assert payment.is_pix
    assert payment.pix_qr_payload
    assert payment.checkout_url.endswith(f"/associe-se/sucesso/{member_id}")


# ---------------------------- against Asaas --------------------------------
@pytest.fixture
def asaas(monkeypatch):
    monkeypatch.setattr(settings, "payment_provider", "asaas")
    monkeypatch.setattr(settings, "asaas_api_key", "test-key")
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


def test_asaas_switch_updates_the_subscription_and_pending_invoices(client, asaas):
    def handler(request):
        path = request.url.path
        if request.method == "PUT" and path.endswith("/subscriptions/sub_1"):
            return httpx.Response(200, json={"id": "sub_1", "customer": "cus_1"})
        if request.method == "GET" and path.endswith("/payments"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "pay_2",
                            "status": "PENDING",
                            "billingType": "PIX",
                            "dueDate": "2026-10-03",
                            "value": 199.9,
                            "invoiceUrl": "https://www.asaas.com/i/pay_2",
                        },
                        {
                            "id": "pay_1",
                            "status": "OVERDUE",
                            "billingType": "CREDIT_CARD",
                            "dueDate": "2026-09-03",
                            "value": 199.9,
                            "invoiceUrl": "https://www.asaas.com/i/pay_1",
                        },
                        {
                            "id": "pay_0",
                            "status": "RECEIVED",
                            "billingType": "CREDIT_CARD",
                        },
                    ]
                },
            )
        if request.method == "PUT" and path.endswith("/payments/pay_1"):
            return httpx.Response(200, json={"id": "pay_1"})
        return httpx.Response(500)

    calls = asaas(handler)
    member_id = make_member(provider="asaas", reference="sub_1")
    login(client)
    assert change(client, member_id, "PIX").status_code == 303

    put = calls[0]
    assert put == (
        "PUT",
        "/v3/subscriptions/sub_1",
        {"billingType": "PIX", "updatePendingPayments": True},
    )
    # The overdue invoice the bulk update did not reach is fixed one by one.
    assert ("PUT", "/v3/payments/pay_1") in [(m, p) for m, p, _ in calls]
    assert ("PUT", "/v3/payments/pay_2") not in [(m, p) for m, p, _ in calls]
    payment = payment_of(member_id)
    assert payment.method == "PIX"
    assert payment.external_reference == "sub_1"
    assert payment.checkout_url == "https://www.asaas.com/i/pay_1"  # oldest open


def test_asaas_refusal_changes_nothing(client, asaas):
    asaas(
        lambda request: httpx.Response(
            400, json={"errors": [{"code": "x", "description": "Assinatura removida."}]}
        )
    )
    member_id = make_member(provider="asaas", reference="sub_1")
    login(client)
    response = change(client, member_id, "PIX")
    assert response.status_code == 502
    assert "Assinatura removida." in response.text
    assert payment_of(member_id).method == "CREDIT_CARD"
    assert not [a for a in the(AuditLog) if a.action == "trocou_pagamento"]


def test_asaas_leaving_pix_automatic_opens_subscription_then_cancels(
    client, asaas, monkeypatch
):
    monkeypatch.setattr(settings, "pix_automatic_enabled", True)

    def handler(request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/subscriptions"):
            return httpx.Response(200, json={"id": "sub_9"})
        if request.method == "GET" and path.endswith("/payments"):
            return httpx.Response(
                200, json={"data": [{"invoiceUrl": "https://www.asaas.com/i/new"}]}
            )
        if request.method == "DELETE":
            return httpx.Response(200, json={"deleted": True})
        return httpx.Response(500)

    calls = asaas(handler)
    member_id = make_member(provider="asaas", reference="auth_1", method="PIX_AUTOMATIC")
    login(client)
    assert change(client, member_id, "PIX").status_code == 303

    order = [(m, p) for m, p, _ in calls]
    assert order.index(("POST", "/v3/subscriptions")) < order.index(
        ("DELETE", "/v3/pix/automatic/authorizations/auth_1")
    )
    [body] = [b for m, p, b in calls if m == "POST" and p == "/v3/subscriptions"]
    assert body["billingType"] == "PIX"
    assert body["customer"] == "cus_1"  # the same customer, not a new one
    payment = payment_of(member_id)
    assert (payment.method, payment.external_reference) == ("PIX", "sub_9")
    assert payment.mandate_status is None
    assert payment.pix_qr_payload is None


def test_asaas_new_charge_is_rolled_back_when_the_old_cannot_be_closed(
    client, asaas, monkeypatch
):
    monkeypatch.setattr(settings, "pix_automatic_enabled", True)

    def handler(request):
        path = request.url.path
        if request.method == "POST" and path.endswith("/pix/automatic/authorizations"):
            return httpx.Response(
                200,
                json={"id": "auth_new", "immediateQrCode": {"payload": "000201"}},
            )
        if request.method == "DELETE" and path.endswith("/subscriptions/sub_1"):
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"deleted": True})

    calls = asaas(handler)
    member_id = make_member(provider="asaas", reference="sub_1")
    login(client)
    assert change(client, member_id, "PIX_AUTOMATIC").status_code == 502
    assert ("DELETE", "/v3/pix/automatic/authorizations/auth_new") in [
        (m, p) for m, p, _ in calls
    ]
    payment = payment_of(member_id)
    assert (payment.method, payment.external_reference) == ("CREDIT_CARD", "sub_1")

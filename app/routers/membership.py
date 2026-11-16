"""Public signup: the two forms, the success page and the mock checkout."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import BrandLogo, Member, MemberStatus, Payment, PaymentStatus
from ..plans import CHAPTERS, CYCLES, GENDERS, PLANS, STATES, plans_for
from ..schemas import CompanySignup, IndividualSignup
from ..security import client_ip
from ..services import marcas
from ..services.members import BusinessError, register_signup
from ..services.payments import PaymentError
from ..templating import templates

log = logging.getLogger("ccpa.membership")
router = APIRouter()

BENEFIT_CARDS = [
    {
        "title": "Diagnóstico Criativo",
        "text": "Uma análise inicial para freelancers, pequenos negócios e "
        "profissionais independentes entenderem melhor seu posicionamento, "
        "desafios e oportunidades no mercado criativo paraense.",
        "color": "verde",
    },
    {
        "title": "Portfolio Review",
        "text": "Tenha seu portfólio avaliado por profissionais premiados e "
        "atuantes no mercado nacional e internacional. Feedbacks qualificados "
        "para estudantes e profissionais que querem evoluir, fortalecer sua "
        "apresentação e ampliar oportunidades.",
        "color": "vermelho",
    },
    {
        "title": "40% de desconto",
        "text": "Associados do CCPA têm 40% de desconto em coleções de camisas "
        "paraenses, com estampas exclusivas criadas por artistas locais, "
        "valorizando a criatividade, a cultura e a identidade do Pará.",
        "color": "azul",
    },
    {
        "title": "Égua, Bora!",
        "text": "Encontros e rodadas de networking para aproximar estudantes, "
        "criativos, profissionais, empreendedores, agências e empresas, "
        "estimulando parcerias, negócios e novas oportunidades no mercado.",
        "color": "amarelo",
    },
]


TABS = ("estudante", "profissional", "pj")


def _tier(code: str) -> dict:
    return {
        c: [p for p in plans_for("PF", c) if p.code.split("_")[0] == code]
        for c, _ in CYCLES
    }


def _tab_for(tipo: str) -> str:
    """The tab the page opens on. `pf` is what links made before the third
    tab existed still say; it meant the individual form, whose first tab is
    estudante."""
    tipo = (tipo or "").lower()
    return tipo if tipo in TABS else "estudante"


def _individual_tab(form: dict) -> str:
    """Which individual tab a submitted form came from — told by its plan."""
    code = str(form.get("plan_code") or "")
    return "profissional" if code.startswith("PROFISSIONAL") else "estudante"


def _base_context(request: Request, db: Session | None = None) -> dict:
    logos = marcas.public_logos(db) if db is not None else []
    return {
        "request": request,
        # Both cycles are rendered; the toggle only decides which one is on
        # screen, so the page still works with JavaScript switched off.
        "individual_plans": {c: plans_for("PF", c) for c, _ in CYCLES},
        # Estudante and profissional each have their own tab, with only their
        # own plan in it.
        "student_plans": _tier("ESTUDANTE"),
        "professional_plans": _tier("PROFISSIONAL"),
        "company_plans": {c: plans_for("PJ", c) for c, _ in CYCLES},
        "cycles": CYCLES,
        "chapters": CHAPTERS,
        "genders": GENDERS,
        "states": STATES,
        "benefits": BENEFIT_CARDS,
        "pix_enabled": settings.pix_automatic_enabled,
        "logos": logos,
        "logos_marquee": len(logos) >= marcas.MARQUEE_MIN,
        "logos_copies": marcas.marquee_copies(len(logos)),
    }


def _clean_form(form: dict) -> dict:
    """HTML sends empty fields as "".

    Dropping them lets Pydantic apply the declared default (None for optional
    fields) or report a missing required field, instead of trying to parse an
    empty string as a date or an e-mail.
    """
    cleaned = {}
    for key, value in form.items():
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                continue
        cleaned[key] = value
    return cleaned


# Pydantic reports in English; the visitor reads Portuguese.
_MESSAGE_MAP = (
    ("Field required", "Campo obrigatório."),
    ("valid email address", "E-mail inválido."),
    ("valid date", "Data inválida."),
    ("at least", "Valor muito curto."),
    ("at most", "Valor muito longo."),
    ("should be a valid", "Valor inválido."),
)


def _translate(message: str) -> str:
    message = message.replace("Value error, ", "")
    for fragment, translation in _MESSAGE_MAP:
        if fragment in message:
            return translation
    return message


def _field_errors(exc: ValidationError) -> dict[str, str]:
    errors: dict[str, str] = {}
    for error in exc.errors():
        field = str(error["loc"][0]) if error["loc"] else "_general"
        errors.setdefault(field, _translate(error["msg"]))
    return errors


@router.get("/associe-se", response_class=HTMLResponse)
def signup_page(request: Request, tipo: str = "estudante", db: Session = Depends(get_db)):
    context = _base_context(request, db)
    context.update({"tab": _tab_for(tipo), "errors": {}, "form": {}})
    return templates.TemplateResponse(request, "membership.html", context)


def _visible_errors(errors: dict[str, str], form: dict) -> dict[str, str]:
    """Make sure every error has somewhere on the page to appear.

    An error keyed to a name that is not an input of the submitted form
    renders nowhere: the visitor reads "check the highlighted fields", finds
    nothing highlighted, and gives up. Those messages move to the box at the
    top of the form instead.
    """
    shown = {"plan_code", "accepted_terms", *form.keys()}
    visible, orphans = {}, []
    for field, message in errors.items():
        if field in shown:
            visible[field] = message
        else:
            orphans.append(message)
    if orphans:
        visible["_general"] = " ".join(orphans)
    return visible


def _handle_submission(
    request: Request, db: Session, form: dict, schema, tab: str
) -> HTMLResponse:
    def refuse(errors: dict[str, str], status: int) -> HTMLResponse:
        context = _base_context(request, db)
        context.update(
            {"tab": tab, "errors": _visible_errors(errors, form), "form": form}
        )
        return templates.TemplateResponse(
            request, "membership.html", context, status_code=status
        )

    try:
        data = schema(**_clean_form(form))
    except ValidationError as exc:
        errors = _field_errors(exc)
        # Field names only: the values are personal data and stay out of logs.
        log.info("Signup %s refused, invalid fields: %s", tab, sorted(errors))
        return refuse(errors, 422)

    try:
        member, _ = register_signup(db, data, ip=client_ip(request))
    except BusinessError as exc:
        log.info("Signup %s refused (%s): %s", tab, exc.field, exc.message)
        return refuse({exc.field: exc.message}, 409)
    except PaymentError as exc:
        # The member was only flushed, never committed: rolling back leaves
        # nothing behind, and the visitor can simply send the form again.
        db.rollback()
        log.warning("Signup %s failed at the gateway: %s", tab, exc.detail)
        return refuse({"_general": exc.message}, 502)

    return RedirectResponse(f"/associe-se/sucesso/{member.id}", status_code=303)


@router.post("/associe-se/pf")
async def submit_individual(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    form["accepted_terms"] = form.get("accepted_terms") in ("on", "true", "1")
    return _handle_submission(request, db, form, IndividualSignup, _individual_tab(form))


@router.post("/associe-se/pj")
async def submit_company(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    form["accepted_terms"] = form.get("accepted_terms") in ("on", "true", "1")
    return _handle_submission(request, db, form, CompanySignup, "pj")


@router.get("/marcas/{logo_id}", include_in_schema=False)
def brand_logo(logo_id: int, db: Session = Depends(get_db)):
    """A member company's logo.

    The address carries `?v=<last change>`, so the image can be cached for a
    year: replacing a logo changes the address, not the cached file.
    """
    logo = db.get(BrandLogo, logo_id)
    if logo is None or not logo.active:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(
        content=marcas.rendered(logo),
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            # Served from our own domain: never let a browser reinterpret it.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/associe-se/sucesso/{member_id}", response_class=HTMLResponse)
def signup_success(request: Request, member_id: str, db: Session = Depends(get_db)):
    member = db.get(Member, member_id)
    if not member:
        return RedirectResponse("/associe-se", status_code=302)

    payment = (
        db.query(Payment)
        .filter(Payment.member_id == member.id)
        .order_by(Payment.created_at.desc())
        .first()
    )
    return templates.TemplateResponse(
        request,
        "success.html",
        {
            "request": request,
            "member": member,
            "plan": PLANS[member.plan_code],
            "payment": payment,
            # Without e-mail this page IS the delivery of the payment link, and
            # it has to say so rather than point at an inbox.
            "email_enabled": settings.email_enabled,
            **_pix_context(payment),
            "pix_invoice": bool(payment and payment.is_pix_invoice),
            "is_mock": settings.payment_provider == "mock",
            "pix_renewed": request.query_params.get("novo") == "1",
            "pix_error": request.query_params.get("erro_pix"),
        },
    )


def _pix_context(payment: Payment | None) -> dict:
    """Whether the Pix QR code on the page can still be paid.

    It stops being payable when it expires (24 hours) or when the bank
    refused or dropped the authorization before the first payment. In both
    cases the page offers a new one instead of a dead code.
    """
    if not payment or not payment.is_pix:
        return {"pix": False}
    from ..services.painel import local

    expires = payment.pix_qr_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    expired = expires is not None and expires <= datetime.now(UTC)
    dead = payment.mandate_status in ("REFUSED", "CANCELLED", "EXPIRED")
    return {
        "pix": True,
        "pix_payable": payment.status != PaymentStatus.CONFIRMED
        and not expired
        and not dead,
        "pix_can_renew": payment.status != PaymentStatus.CONFIRMED and (expired or dead),
        "pix_expires_text": local(expires).strftime("%d/%m às %Hh%M")
        if expires
        else None,
    }


@router.post("/associe-se/sucesso/{member_id}/novo-pix")
def renew_pix_qr(member_id: str, db: Session = Depends(get_db)):
    """Replace an expired or refused QR code with a fresh one.

    Public, like the page it lives on: the member id in the address is the
    only credential, as it is for the payment link itself. Only a QR code
    that can no longer be paid is ever replaced.
    """
    member = db.get(Member, member_id)
    if not member:
        return RedirectResponse("/associe-se", status_code=302)
    payment = (
        db.query(Payment)
        .filter(Payment.member_id == member.id)
        .order_by(Payment.created_at.desc())
        .first()
    )
    back = f"/associe-se/sucesso/{member.id}"
    if not payment or not _pix_context(payment).get("pix_can_renew"):
        return RedirectResponse(back, status_code=303)

    from ..services.members import apply_checkout
    from ..services.payments import get_payment_provider

    try:
        link = get_payment_provider().renew_pix(member, PLANS[member.plan_code], payment)
    except PaymentError as exc:
        log.warning("Pix renewal failed for %s: %s", member.id, exc.detail)
        return RedirectResponse(f"{back}?erro_pix=1", status_code=303)

    apply_checkout(payment, link, member)
    payment.status = PaymentStatus.PENDING
    db.commit()
    return RedirectResponse(f"{back}?novo=1", status_code=303)


@router.get("/pagamento/simulado/{member_id}", response_class=HTMLResponse)
def mock_checkout(request: Request, member_id: str, db: Session = Depends(get_db)):
    """Fake checkout screen. Only reachable with PAYMENT_PROVIDER=mock."""
    from ..config import settings

    if settings.payment_provider != "mock":
        return RedirectResponse("/associe-se", status_code=302)

    member = db.get(Member, member_id)
    if not member:
        return RedirectResponse("/associe-se", status_code=302)

    return templates.TemplateResponse(
        request,
        "mock_payment.html",
        {"request": request, "member": member, "plan": PLANS[member.plan_code]},
    )


@router.post("/pagamento/simulado/{member_id}/confirmar")
def confirm_mock_payment(member_id: str, db: Session = Depends(get_db)):
    from datetime import datetime

    from ..config import settings
    from ..services.billing import record_charge
    from ..services.email import send_payment_confirmed

    if settings.payment_provider != "mock":
        return RedirectResponse("/associe-se", status_code=302)

    member = db.get(Member, member_id)
    if not member:
        return RedirectResponse("/associe-se", status_code=302)

    payment = (
        db.query(Payment)
        .filter(Payment.member_id == member.id)
        .order_by(Payment.created_at.desc())
        .first()
    )
    if payment:
        payment.status = PaymentStatus.CONFIRMED
        payment.paid_at = datetime.now(UTC)
        if payment.is_pix:
            payment.mandate_status = "ACTIVE"  # the bank's confirmation, simulated
        # The same record the Asaas webhook would leave, so the panel's
        # revenue figures can be tried out in development.
        record_charge(
            db,
            member,
            {
                "id": f"mock_pay_{payment.id[:12]}",
                "value": payment.amount_cents / 100,
                "status": "CONFIRMED",
                "dueDate": datetime.now(UTC).date().isoformat(),
            },
        )
    member.status = MemberStatus.ACTIVE
    db.commit()

    send_payment_confirmed(db, member, PLANS[member.plan_code])
    return RedirectResponse(f"/associe-se/sucesso/{member.id}", status_code=303)

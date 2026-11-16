"""Management panel — /painel.

How the area is protected:
  * a signed session cookie, HttpOnly, SameSite=Lax, Secure over HTTPS;
  * the account's session version inside the cookie, so a password change or
    a deactivation ends every open session at once;
  * a CSRF token on every POST (double-submit cookie);
  * five wrong passwords for an e-mail in fifteen minutes lock it for the
    rest of that window — counted in the database, so it holds across
    serverless instances, which an in-memory counter would not;
  * every route takes `require_user` or `require_admin`, so none can be left
    open by forgetting a check;
  * pages are `no-store` and `noindex`: member data must not sit in a shared
    computer's cache or in a search engine.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from math import ceil
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import (
    AdminRole,
    AdminUser,
    AuditLog,
    BrandLogo,
    EmailLog,
    EntityType,
    Member,
    MemberStatus,
    PaymentStatus,
)
from ..plans import CHAPTERS, PLANS
from ..security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    create_session_token,
    csrf_matches,
    generate_password,
    hash_password,
    new_csrf_token,
    read_session_token,
    verify_password,
)
from ..services import marcas
from ..services import painel as svc
from ..services.billing import CHARGE_LABELS, PAID, REVERSED, reconcile
from ..services.email import send_payment_confirmed
from ..services.members import apply_checkout
from ..services.payments import (
    CARD,
    METHOD_LABELS,
    PIX,
    PIX_AUTOMATIC,
    AsaasProvider,
    PaymentError,
    get_payment_provider,
)
from ..templating import templates

log = logging.getLogger("ccpa.painel")
router = APIRouter(prefix="/painel", include_in_schema=False)

MIN_PASSWORD = 10
MAX_FAILED_LOGINS = 5


# --------------------------------------------------------------------------
#  Access control
# --------------------------------------------------------------------------
class LoginRequired(Exception):
    def __init__(self, next_path: str = "/painel"):
        self.next_path = next_path


class PasswordChangeRequired(Exception):
    pass


class Forbidden(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> AdminUser | None:
    session = read_session_token(request.cookies.get(SESSION_COOKIE))
    if not session:
        return None
    user_id, version = session
    user = db.get(AdminUser, user_id)
    if not user or not user.active or user.session_version != version:
        return None
    return user


def require_user(
    request: Request, user: AdminUser | None = Depends(current_user)
) -> AdminUser:
    if user is None:
        raise LoginRequired(_safe_next(request.url.path))
    if user.must_change_password and request.url.path != "/painel/senha":
        raise PasswordChangeRequired()
    return user


def require_admin(user: AdminUser = Depends(require_user)) -> AdminUser:
    if not user.is_admin:
        raise Forbidden()
    return user


def _safe_next(path: str | None) -> str:
    # Only ever back into the panel: an open redirect on a login page is a
    # phishing tool.
    if path and path.startswith("/painel") and not path.startswith("//"):
        return path
    return "/painel"


def _csrf(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or new_csrf_token()


async def _form(request: Request) -> dict:
    form = dict(await request.form())
    if not csrf_matches(request.cookies.get(CSRF_COOKIE), form.get("csrf")):
        raise Forbidden()
    return form


def render(
    request: Request,
    template: str,
    context: dict,
    *,
    user: AdminUser | None = None,
    status: int = 200,
) -> HTMLResponse:
    csrf = _csrf(request)
    base = {
        "request": request,
        "user": user,
        "csrf": csrf,
        "path": request.url.path,
        "status_labels": svc.STATUS_LABELS,
        "status_tones": svc.STATUS_TONES,
        "plans": PLANS,
        "local": svc.local,
        **svc.FORMATTERS,
    }
    base.update(context)
    response = templates.TemplateResponse(request, template, base, status_code=status)
    if request.cookies.get(CSRF_COOKIE) != csrf:
        response.set_cookie(
            CSRF_COOKIE,
            csrf,
            # Readable on purpose: the page echoes it into each form. The
            # protection is that another site cannot read it to copy it.
            httponly=False,
            samesite="strict",
            secure=request.url.scheme == "https",
            max_age=SESSION_MAX_AGE,
            path="/painel",
        )
    return response


def _session_cookie(response: Response, request: Request, user: AdminUser) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user.id, user.session_version),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=SESSION_MAX_AGE,
        path="/painel",
    )


# --------------------------------------------------------------------------
#  Sign in / out / password
# --------------------------------------------------------------------------
@router.get("/entrar", response_class=HTMLResponse)
def login_page(
    request: Request,
    proxima: str = "/painel",
    user: AdminUser | None = Depends(current_user),
):
    if user:
        return RedirectResponse(_safe_next(proxima), status_code=303)
    return render(
        request, "painel/entrar.html", {"error": None, "proxima": _safe_next(proxima)}
    )


@router.post("/entrar")
async def do_login(request: Request, db: Session = Depends(get_db)):
    form = await _form(request)
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    next_path = _safe_next(form.get("proxima"))

    def refuse(message: str, status: int = 401):
        return render(
            request,
            "painel/entrar.html",
            {"error": message, "proxima": next_path, "email": email},
            status=status,
        )

    if svc.recent_failed_logins(db, email) >= MAX_FAILED_LOGINS:
        svc.audit(db, request, None, "login_bloqueado", target=email)
        return refuse(
            "Muitas tentativas erradas. Aguarde 15 minutos e tente de novo.", 429
        )

    user = db.scalar(select(AdminUser).where(func.lower(AdminUser.email) == email))

    # One message for every failure: saying which part was wrong would tell a
    # stranger which e-mails have an account.
    if not user or not user.active or not verify_password(user.password_hash, password):
        svc.audit(db, request, None, "login_falhou", target=email)
        return refuse("E-mail ou senha incorretos.")

    user.last_login_at = datetime.now(UTC)
    db.commit()
    svc.audit(db, request, user, "entrou")

    response = RedirectResponse(
        "/painel/senha" if user.must_change_password else next_path, status_code=303
    )
    _session_cookie(response, request, user)
    return response


@router.post("/sair")
async def do_logout(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser | None = Depends(current_user),
):
    await _form(request)
    if user:
        svc.audit(db, request, user, "saiu")
    response = RedirectResponse("/painel/entrar", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/painel")
    return response


@router.get("/senha", response_class=HTMLResponse)
def password_page(request: Request, user: AdminUser = Depends(require_user)):
    return render(request, "painel/senha.html", {"errors": {}}, user=user)


@router.post("/senha")
async def change_password(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_user),
):
    form = await _form(request)
    current = form.get("atual") or ""
    new = form.get("nova") or ""
    confirmation = form.get("confirmacao") or ""

    errors = {}
    if not verify_password(user.password_hash, current):
        errors["atual"] = "Senha atual incorreta."
    if len(new) < MIN_PASSWORD:
        errors["nova"] = f"Use pelo menos {MIN_PASSWORD} caracteres."
    elif new == current:
        errors["nova"] = "A nova senha precisa ser diferente da atual."
    elif user.email.lower() in new.lower():
        errors["nova"] = "A senha não pode conter o seu e-mail."
    if new != confirmation:
        errors["confirmacao"] = "As duas senhas não conferem."
    if errors:
        return render(
            request, "painel/senha.html", {"errors": errors}, user=user, status=422
        )

    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.session_version += 1  # every other open session ends here
    db.commit()
    svc.audit(db, request, user, "trocou_senha")

    response = RedirectResponse("/painel?senha=ok", status_code=303)
    _session_cookie(response, request, user)
    return response


# --------------------------------------------------------------------------
#  Overview
# --------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
def overview(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_user),
):
    attention = svc.needs_attention(db)
    return render(
        request,
        "painel/visao.html",
        {
            "o": svc.overview(db),
            "attention": [(m, svc.latest_payment(db, m)) for m in attention],
            "recent": list(
                db.scalars(select(Member).order_by(Member.created_at.desc()).limit(8))
            ),
            "senha_ok": request.query_params.get("senha") == "ok",
            "provider": settings.payment_provider,
        },
        user=user,
    )


# --------------------------------------------------------------------------
#  Members
# --------------------------------------------------------------------------
@router.get("/associados", response_class=HTMLResponse)
def members(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_user),
):
    f = svc.Filters.from_query(request.query_params)
    rows, total = svc.list_members(db, f)
    return render(
        request,
        "painel/associados.html",
        {
            "f": f,
            "rows": rows,
            "total": total,
            "pages": max(1, ceil(total / svc.PAGE_SIZE)),
            "tiers": svc.TIERS,
            "chapters": CHAPTERS,
            "statuses": list(svc.STATUS_LABELS.items()),
            "deleted": request.query_params.get("excluido"),
        },
        user=user,
    )


@router.get("/associados/exportar.csv")
def export(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    f = svc.Filters.from_query(request.query_params)
    body = svc.export_csv(db, f)
    rows = body.count("\r\n") - 1
    svc.audit(
        db,
        request,
        user,
        "exportou",
        detail=f"{rows} associado(s)"
        + (f" · filtros: {f.query_string()}" if f.active else ""),
    )
    stamp = datetime.now(UTC).astimezone(svc.TZ).strftime("%Y-%m-%d_%Hh%M")
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="associados-ccpa_{stamp}.csv"'
        },
    )


@router.get("/associados/{member_id}", response_class=HTMLResponse)
def member_detail(
    member_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_user),
):
    member = db.get(Member, member_id)
    if member is None:
        return RedirectResponse("/painel/associados", status_code=303)

    svc.audit(db, request, user, "viu_ficha", target=member.id, detail=member.name)
    payment = svc.latest_payment(db, member)

    access = []
    if user.is_admin:
        access = list(
            db.scalars(
                select(AuditLog)
                .where(AuditLog.target == member.id)
                .order_by(AuditLog.created_at.desc())
                .limit(10)
            )
        )

    return render(
        request,
        "painel/ficha.html",
        {
            "m": member,
            "plan": PLANS.get(member.plan_code),
            "payment": payment,
            "charges": sorted(
                member.charges, key=lambda c: c.due_date or "", reverse=True
            ),
            "charge_labels": CHARGE_LABELS,
            "paid_statuses": PAID,
            "reversed_statuses": REVERSED,
            "emails": list(
                db.scalars(
                    select(EmailLog)
                    .where(EmailLog.member_id == member.id)
                    .order_by(EmailLog.created_at.desc())
                )
            ),
            "access": access,
            "action_labels": svc.ACTION_LABELS,
            "can_charge": member.status
            in (MemberStatus.AWAITING_PAYMENT, MemberStatus.OVERDUE),
            "has_whatsapp": bool(svc.whatsapp_url(member, "")),
            "method_changed": request.query_params.get("pagamento") == "ok",
            "logos": list(
                db.scalars(select(BrandLogo).where(BrandLogo.member_id == member.id))
            )
            if user.is_admin
            else [],
            "logo_ok": request.query_params.get("logo"),
            "logo_error": request.query_params.get("logo_erro"),
        },
        user=user,
    )


@router.get("/associados/{member_id}/whatsapp")
def open_whatsapp(
    member_id: str,
    request: Request,
    tipo: str = "contato",
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_user),
):
    member = db.get(Member, member_id)
    if member is None:
        return RedirectResponse("/painel/associados", status_code=303)
    kind = "cobranca" if tipo == "cobranca" else "contato"
    url = svc.whatsapp_url(
        member, svc.whatsapp_message(member, svc.latest_payment(db, member), kind)
    )
    if url is None:
        return RedirectResponse(f"/painel/associados/{member.id}", status_code=303)
    svc.audit(
        db,
        request,
        user,
        "whatsapp",
        target=member.id,
        detail=f"{member.name} · {'cobrança' if kind == 'cobranca' else 'contato'}",
    )
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------
#  Changing the payment method (admin only)
# --------------------------------------------------------------------------
def _method_options(member: Member, payment) -> list[dict]:
    """The methods the member can be moved to, and why one is unavailable.

    Pix Automático is only offered to a member who has not paid anything
    yet: joining it means paying a QR code for the current period, which
    would charge someone who already paid a second time.
    """
    paid = member.status != MemberStatus.AWAITING_PAYMENT or (
        payment is not None and payment.status == PaymentStatus.CONFIRMED
    )
    options = [
        {
            "value": CARD,
            "label": METHOD_LABELS[CARD],
            "desc": "Cobrado automaticamente no cartão a cada período. O associado "
            "cadastra o cartão na página da Asaas.",
        },
        {
            "value": PIX,
            "label": METHOD_LABELS[PIX],
            "desc": "A cada período a Asaas gera uma cobrança com QR Code Pix e "
            "avisa o associado; ele paga pelo app do banco.",
        },
    ]
    automatic = {
        "value": PIX_AUTOMATIC,
        "label": METHOD_LABELS[PIX_AUTOMATIC],
        "desc": "Paga o primeiro período com QR Code e autoriza no banco o débito "
        "automático dos próximos.",
    }
    if not settings.pix_automatic_enabled:
        # Shown, but off: the team should see the option exists and what is
        # missing, rather than wonder where it went.
        automatic["blocked"] = (
            "Desligado: falta a Asaas liberar o Pix Automático na conta e "
            "ligar PIX_AUTOMATIC_ENABLED na Vercel."
        )
    elif paid and (payment is None or payment.method != PIX_AUTOMATIC):
        automatic["blocked"] = (
            "Só para quem ainda não pagou: a entrada exige pagar um QR Code do "
            "período atual, o que cobraria de novo."
        )
    options.append(automatic)
    if payment is not None and payment.method == PIX_AUTOMATIC and paid:
        # Leaving it opens a new subscription whose first invoice is due in
        # a few days — the same double charge, the other way round.
        for option in options[:2]:
            option["blocked"] = (
                "Associado já pagou pelo Pix Automático: para trocar, cancele a "
                "autorização na Asaas e combine a próxima cobrança com ele."
            )
    return options


def _method_page(
    request: Request,
    db: Session,
    user: AdminUser,
    member: Member,
    *,
    error: str | None = None,
    form: dict | None = None,
    status: int = 200,
) -> HTMLResponse:
    payment = svc.latest_payment(db, member)
    return render(
        request,
        "painel/pagamento.html",
        {
            "m": member,
            "plan": PLANS.get(member.plan_code),
            "payment": payment,
            "options": _method_options(member, payment),
            "current": payment.method if payment else None,
            "error": error,
            "form": form or {},
        },
        user=user,
        status=status,
    )


@router.get("/associados/{member_id}/pagamento", response_class=HTMLResponse)
def payment_method_page(
    member_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    member = db.get(Member, member_id)
    if member is None:
        return RedirectResponse("/painel/associados", status_code=303)
    return _method_page(request, db, user, member)


@router.post("/associados/{member_id}/pagamento", response_class=HTMLResponse)
async def change_payment_method(
    member_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    form = await _form(request)
    member = db.get(Member, member_id)
    if member is None:
        return RedirectResponse("/painel/associados", status_code=303)
    payment = svc.latest_payment(db, member)

    def refuse(message: str, status: int = 422) -> HTMLResponse:
        return _method_page(
            request, db, user, member, error=message, form=form, status=status
        )

    if payment is None:
        return refuse("Este cadastro não tem cobrança aberta para alterar.", 409)
    if member.status == MemberStatus.CANCELLED:
        return refuse("Associação cancelada: não há cobrança para alterar.", 409)

    new_method = form.get("metodo") or ""
    option = next(
        (o for o in _method_options(member, payment) if o["value"] == new_method), None
    )
    if option is None:
        return refuse("Escolha a nova forma de pagamento.")
    if new_method == payment.method:
        return refuse("Esta já é a forma de pagamento atual.")
    if option.get("blocked"):
        return refuse(option["blocked"], 409)
    reason = (form.get("motivo") or "").strip()
    if len(reason) < 3:
        return refuse("Escreva o motivo — ele fica no registro de acessos.")

    plan = PLANS[member.plan_code]
    before = payment.method
    try:
        link = get_payment_provider().change_method(member, plan, payment, new_method)
    except PaymentError as exc:
        log.warning("Method change for %s failed: %s", member.id, exc.detail)
        return refuse(exc.message, 502)

    apply_checkout(payment, link, member)
    if new_method == PIX_AUTOMATIC or before == PIX_AUTOMATIC:
        # A new charge replaced the old one: its first period is still unpaid.
        payment.status = PaymentStatus.PENDING
    db.commit()
    svc.audit(
        db, request, user, "trocou_pagamento", target=member.id,
        detail=f"{member.trade_name or member.name} · {METHOD_LABELS.get(before, before)}"
        f" → {METHOD_LABELS[new_method]} · motivo: {reason[:300]}",
    )  # fmt: skip
    return RedirectResponse(
        f"/painel/associados/{member.id}?pagamento=ok", status_code=303
    )


# --------------------------------------------------------------------------
#  Deleting a record (admin only)
# --------------------------------------------------------------------------
CONFIRM_WORD = "EXCLUIR"


def _deletion_blocker(member: Member) -> str | None:
    """Why this record must not be deleted, if there is a reason.

    A member who paid is a financial record: deleting it would make money in
    the Asaas account impossible to trace back to anyone. Refund or cancel in
    Asaas first; "Conferir com a Asaas" then marks the charge as reversed and
    the record becomes deletable.
    """
    paid = any(c.status in PAID for c in member.charges) or any(
        p.status == PaymentStatus.CONFIRMED for p in member.payments
    )
    if paid:
        return (
            "Este cadastro tem pagamento confirmado. Estorne ou cancele a "
            "cobrança na Asaas, clique em “Conferir com a Asaas” na visão geral "
            "e depois volte aqui."
        )
    return None


def _delete_page(
    request: Request,
    db: Session,
    user: AdminUser,
    member: Member,
    *,
    error: str | None = None,
    form: dict | None = None,
    status: int = 200,
) -> HTMLResponse:
    payment = svc.latest_payment(db, member)
    return render(
        request,
        "painel/excluir.html",
        {
            "m": member,
            "plan": PLANS.get(member.plan_code),
            "payment": payment,
            "blocker": _deletion_blocker(member),
            "in_asaas": bool(
                payment and payment.provider == "asaas" and payment.external_reference
            ),
            "error": error,
            "form": form or {},
            "confirm_word": CONFIRM_WORD,
        },
        user=user,
        status=status,
    )


@router.get("/associados/{member_id}/excluir", response_class=HTMLResponse)
def delete_member_page(
    member_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    member = db.get(Member, member_id)
    if member is None:
        return RedirectResponse("/painel/associados", status_code=303)
    return _delete_page(request, db, user, member)


@router.post("/associados/{member_id}/excluir", response_class=HTMLResponse)
async def delete_member(
    member_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    form = await _form(request)
    member = db.get(Member, member_id)
    if member is None:
        return RedirectResponse("/painel/associados", status_code=303)

    def refuse(message: str, status: int = 422) -> HTMLResponse:
        return _delete_page(
            request, db, user, member, error=message, form=form, status=status
        )

    blocker = _deletion_blocker(member)
    if blocker:
        return refuse(blocker, 409)
    reason = (form.get("motivo") or "").strip()
    if len(reason) < 3:
        return refuse("Escreva o motivo da exclusão — ele fica no registro de acessos.")
    if (form.get("confirmacao") or "").strip().upper() != CONFIRM_WORD:
        return refuse(f"Para confirmar, digite {CONFIRM_WORD} no campo indicado.")

    # Asaas first: if it refuses, the local record stays, rather than leaving
    # a live subscription that nobody can see charging someone every month.
    payment = svc.latest_payment(db, member)
    note = "sem assinatura na Asaas"
    if payment and payment.provider == "asaas" and payment.external_reference:
        try:
            if payment.is_pix:
                note = AsaasProvider().cancel_pix_authorization(
                    payment.external_reference, payment.provider_customer_id
                )
            else:
                note = AsaasProvider().cancel_subscription(payment.external_reference)
        except PaymentError as exc:
            log.warning("Delete %s blocked by Asaas: %s", member.id, exc.detail)
            return refuse(exc.message, 502)

    name = member.trade_name or member.name
    plan = PLANS.get(member.plan_code)
    detail = (
        f"{name} · {plan.name if plan else member.plan_code} · "
        f"cadastro de {svc.fmt_moment(member.created_at, '%d/%m/%Y')} · "
        f"{note} · motivo: {reason[:300]}"
    )
    member_ref = member.id
    db.query(EmailLog).filter(EmailLog.member_id == member.id).delete()
    # The logo stays on the page (the company may still be a partner); it
    # just stops pointing at a record that no longer exists.
    db.query(BrandLogo).filter(BrandLogo.member_id == member.id).update(
        {BrandLogo.member_id: None}
    )
    db.delete(member)  # payments and charges go with it (cascade)
    db.commit()
    svc.audit(db, request, user, "excluiu_associado", target=member_ref, detail=detail)
    return RedirectResponse(
        f"/painel/associados?{urlencode({'excluido': name})}", status_code=303
    )


# --------------------------------------------------------------------------
#  Reconciliation with Asaas
# --------------------------------------------------------------------------
@router.post("/conferir", response_class=HTMLResponse)
async def check_with_asaas(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_user),
):
    await _form(request)
    report = reconcile(db)

    for change in report.changes:
        if (
            change.before == MemberStatus.AWAITING_PAYMENT
            and change.after == MemberStatus.ACTIVE
        ):
            # The confirmation the lost webhook would have sent.
            send_payment_confirmed(db, change.member, PLANS[change.member.plan_code])

    svc.audit(
        db,
        request,
        user,
        "conferiu_asaas",
        detail=report.error
        or (
            f"{report.checked} verificado(s), {len(report.changes)} corrigido(s), "
            f"{report.charges_created} cobrança(s) nova(s)"
        ),
    )
    return render(request, "painel/conferencia.html", {"r": report}, user=user)


# --------------------------------------------------------------------------
#  Users (admin only)
# --------------------------------------------------------------------------
def _users_page(
    request: Request,
    db: Session,
    user: AdminUser,
    *,
    revealed: tuple[AdminUser, str] | None = None,
    errors: dict | None = None,
    form: dict | None = None,
    status: int = 200,
) -> HTMLResponse:
    return render(
        request,
        "painel/usuarios.html",
        {
            "users": list(
                db.scalars(
                    select(AdminUser).order_by(AdminUser.active.desc(), AdminUser.name)
                )
            ),
            "revealed": revealed,
            "errors": errors or {},
            "form": form or {},
            "roles": [
                (r.value, "Administrador" if r == AdminRole.ADMIN else "Equipe")
                for r in AdminRole
            ],  # fmt: skip
        },
        user=user,
        status=status,
    )


@router.get("/usuarios", response_class=HTMLResponse)
def users(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    return _users_page(request, db, user)


@router.post("/usuarios", response_class=HTMLResponse)
async def create_user(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    form = await _form(request)
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    role = form.get("role") if form.get("role") in AdminRole.__members__ else "EQUIPE"

    errors = {}
    if len(name) < 2:
        errors["name"] = "Informe o nome."
    if "@" not in email or "." not in email.split("@")[-1]:
        errors["email"] = "E-mail inválido."
    elif db.scalar(select(AdminUser).where(func.lower(AdminUser.email) == email)):
        errors["email"] = "Já existe um usuário com este e-mail."
    if errors:
        return _users_page(request, db, user, errors=errors, form=form, status=422)

    password = generate_password()
    new_user = AdminUser(
        name=name,
        email=email,
        role=AdminRole[role],
        password_hash=hash_password(password),
        must_change_password=True,
    )
    db.add(new_user)
    db.commit()
    svc.audit(
        db, request, user, "criou_usuario", target=new_user.id,
        detail=f"{name} <{email}> · {role}",
    )  # fmt: skip
    return _users_page(request, db, user, revealed=(new_user, password))


@router.post("/usuarios/{user_id}/ativo", response_class=HTMLResponse)
async def toggle_user(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    await _form(request)
    target = db.get(AdminUser, user_id)
    if target is None or target.id == user.id:
        # Nobody deactivates themselves: that is how a panel ends up with no
        # administrator at all.
        return RedirectResponse("/painel/usuarios", status_code=303)
    target.active = not target.active
    target.session_version += 1
    db.commit()
    svc.audit(
        db,
        request,
        user,
        "reativou_usuario" if target.active else "desativou_usuario",
        target=target.id,
        detail=f"{target.name} <{target.email}>",
    )
    return RedirectResponse("/painel/usuarios", status_code=303)


@router.post("/usuarios/{user_id}/senha", response_class=HTMLResponse)
async def reset_user_password(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    await _form(request)
    target = db.get(AdminUser, user_id)
    if target is None or target.id == user.id:
        return RedirectResponse("/painel/usuarios", status_code=303)
    password = generate_password()
    target.password_hash = hash_password(password)
    target.must_change_password = True
    target.session_version += 1
    db.commit()
    svc.audit(
        db, request, user, "redefiniu_senha", target=target.id,
        detail=f"{target.name} <{target.email}>",
    )  # fmt: skip
    return _users_page(request, db, user, revealed=(target, password))


# --------------------------------------------------------------------------
#  Logos of member companies (admin only)
# --------------------------------------------------------------------------
def _company_members(db: Session) -> list[Member]:
    return list(
        db.scalars(
            select(Member)
            .where(
                Member.entity_type == EntityType.PJ,
                Member.status != MemberStatus.CANCELLED,
            )
            .order_by(func.coalesce(Member.trade_name, Member.name))
        )
    )


def _logos_page(
    request: Request,
    db: Session,
    user: AdminUser,
    *,
    error: str | None = None,
    notice: str | None = None,
    form: dict | None = None,
    status: int = 200,
) -> HTMLResponse:
    logos = marcas.ordered(db)
    companies = _company_members(db)
    return render(
        request,
        "painel/marcas.html",
        {
            "logos": logos,
            "visible": sum(1 for logo in logos if logo.active),
            "marquee_min": marcas.MARQUEE_MIN,
            "max_mb": marcas.MAX_BYTES // (1024 * 1024),
            "companies": companies,
            "member_names": {
                m.id: m.trade_name or m.name
                for m in db.scalars(
                    select(Member).where(
                        Member.id.in_([lg.member_id for lg in logos if lg.member_id])
                    )
                )
            },
            "error": error,
            "notice": notice or request.query_params.get("ok"),
            "form": form or {},
        },
        user=user,
        status=status,
    )


def _logo_owner(db: Session, form: dict) -> tuple[Member | None, str, str]:
    """The linked member (if any), and the name and site to use.

    A logo sent from a member's record, or with a member picked in the list,
    takes that member's trade name and website unless the admin typed
    others — the usual case needs nothing but the file.
    """
    member = None
    member_id = (form.get("associado") or "").strip()
    if member_id:
        member = db.get(Member, member_id)
    name = (form.get("nome") or "").strip()
    site = (form.get("site") or "").strip()
    if member is not None:
        name = name or member.trade_name or member.name
        site = site or member.website or ""
    return member, name, site


def _logo_done(member: Member | None, back_to_record: bool, message: str):
    if back_to_record and member is not None:
        return RedirectResponse(
            f"/painel/associados/{member.id}?{urlencode({'logo': message})}",
            status_code=303,
        )
    return RedirectResponse(
        f"/painel/marcas?{urlencode({'ok': message})}", status_code=303
    )


def _save_logo(
    request: Request,
    db: Session,
    user: AdminUser,
    member: Member | None,
    name: str,
    site: str,
    png: bytes,
) -> BrandLogo:
    logo = marcas.add(db, name, site, png, member.id if member else None)
    svc.audit(
        db, request, user, "adicionou_marca", target=f"marca:{logo.id}",
        detail=f"{logo.name} · {len(png) // 1024} KB"
        + (f" · associado {member.id[:8].upper()}" if member else ""),
    )  # fmt: skip
    return logo


@router.get("/marcas", response_class=HTMLResponse)
def logos_page(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    return _logos_page(request, db, user)


@router.post("/marcas", response_class=HTMLResponse)
async def add_logo(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    form = await _form(request)
    upload = form.get("arquivo")
    data = b""
    if upload is not None and hasattr(upload, "read"):
        # One byte past the limit is enough to know it is too big, without
        # holding a huge upload in memory.
        data = await upload.read(marcas.MAX_BYTES + 1)
    member, name, site = _logo_owner(db, form)
    back_to_record = form.get("voltar") == "ficha"
    text_fields = {
        "nome": form.get("nome") or "",
        "site": form.get("site") or "",
        "associado": member.id if member else "",
    }

    def refuse(message: str):
        if back_to_record and member is not None:
            return RedirectResponse(
                f"/painel/associados/{member.id}?{urlencode({'logo_erro': message})}",
                status_code=303,
            )
        return _logos_page(request, db, user, error=message, form=text_fields, status=422)

    if len(name) < 2:
        return refuse("Informe o nome da empresa ou escolha o associado.")
    try:
        options = marcas.prepare(data)
        if len(options) == 1:
            logo = _save_logo(request, db, user, member, name, site, options[0])
            return _logo_done(member, back_to_record, f"Logo de {logo.name} adicionada.")
    except marcas.LogoError as exc:
        return refuse(str(exc))

    # A PDF with several versions of the logo: let the admin pick one.
    return render(
        request,
        "painel/marcas_escolher.html",
        {
            "options": [marcas.as_data_uri(png) for png in options],
            "nome": name,
            "site": site,
            "member": member,
            "voltar": "ficha" if back_to_record else "",
        },
        user=user,
    )


@router.post("/marcas/escolher", response_class=HTMLResponse)
async def choose_logo_version(
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    form = await _form(request)
    member, name, site = _logo_owner(db, form)
    back_to_record = form.get("voltar") == "ficha"
    try:
        png = marcas.from_choice(form.get("escolha") or "")
        logo = _save_logo(request, db, user, member, name, site, png)
    except marcas.LogoError as exc:
        return _logos_page(request, db, user, error=str(exc), status=422)
    return _logo_done(member, back_to_record, f"Logo de {logo.name} adicionada.")


@router.get("/marcas/{logo_id}/imagem")
def logo_image(
    logo_id: int,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),  # noqa: ARG001
):
    """The image for the panel's list — hidden logos included, which the
    public address does not serve."""
    logo = db.get(BrandLogo, logo_id)
    if logo is None:
        return Response(status_code=404)
    return Response(content=marcas.rendered(logo), media_type="image/png")


@router.post("/marcas/{logo_id}/{acao}", response_class=HTMLResponse)
async def change_logo(
    logo_id: int,
    acao: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    await _form(request)
    logo = db.get(BrandLogo, logo_id)
    if logo is None or acao not in ("subir", "descer", "ativo", "cor", "remover"):
        return RedirectResponse("/painel/marcas", status_code=303)

    name = logo.name
    if acao in ("subir", "descer"):
        if marcas.move(db, logo, -1 if acao == "subir" else 1):
            svc.audit(
                db, request, user, "reordenou_marcas", target=f"marca:{logo_id}",
                detail=f"{name} · {'para cima' if acao == 'subir' else 'para baixo'}",
            )  # fmt: skip
        return RedirectResponse("/painel/marcas", status_code=303)

    if acao == "cor":
        logo.tone = "original" if logo.tone != "original" else "branca"
        db.commit()
        svc.audit(
            db, request, user, "mudou_cor_marca", target=f"marca:{logo_id}",
            detail=f"{name} · {'cores originais' if logo.tone == 'original' else 'branca'}",
        )  # fmt: skip
        return RedirectResponse("/painel/marcas", status_code=303)

    if acao == "ativo":
        logo.active = not logo.active
        db.commit()
        action = "mostrou_marca" if logo.active else "ocultou_marca"
        message = f"{name} {'voltou para' if logo.active else 'saiu de'} a página."
    else:
        db.delete(logo)
        db.commit()
        action, message = "removeu_marca", f"Logo de {name} removida."
    svc.audit(db, request, user, action, target=f"marca:{logo_id}", detail=name)
    return RedirectResponse(
        f"/painel/marcas?{urlencode({'ok': message})}", status_code=303
    )


# --------------------------------------------------------------------------
#  Audit trail (admin only)
# --------------------------------------------------------------------------
@router.get("/registro", response_class=HTMLResponse)
def audit_trail(
    request: Request,
    acao: str = "",
    pagina: int = 1,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    page_size = 50
    pagina = max(1, pagina)
    query = select(AuditLog)
    if acao in svc.ACTION_LABELS:
        query = query.where(AuditLog.action == acao)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    entries = db.scalars(
        query.order_by(AuditLog.created_at.desc())
        .offset((pagina - 1) * page_size)
        .limit(page_size)
    ).all()
    return render(
        request,
        "painel/registro.html",
        {
            "entries": entries,
            "labels": svc.ACTION_LABELS,
            "acao": acao,
            "pagina": pagina,
            "pages": max(1, ceil(total / page_size)),
            "total": total,
        },
        user=user,
    )

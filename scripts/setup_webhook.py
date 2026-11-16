"""Register (or update) the Asaas webhook that activates paid memberships.

    python scripts/setup_webhook.py https://associados.ccpara.com.br

Doing this through the API rather than the Asaas dashboard has two advantages:
the events come straight from the handler that consumes them, so the two cannot
drift apart, and the authentication token is generated here instead of being
invented by hand.

Needs ASAAS_API_KEY in the environment (or in .env). ASAAS_BASE_URL decides
which account is touched — sandbox and production are separate accounts with
separate keys, so check the line the script prints before answering yes.
"""

import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.payments import AsaasProvider  # noqa: E402

WEBHOOK_PATH = "/webhooks/pagamento"

# The events the application actually knows how to act on. Taken from the
# handler's own table so that adding an event there is enough — subscribing to
# an event nothing handles is noise, and handling one nobody sends is a bug
# that only shows up as a membership that never activates.
EVENTS = sorted({*AsaasProvider.STATUS_MAP, *AsaasProvider.AUTHORIZATION_EVENTS})


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.asaas_base_url,
        headers={
            "access_token": settings.asaas_api_key,
            "Content-Type": "application/json",
        },
        timeout=20.0,
    )


def _fail(message: str) -> None:
    print(f"\n  {message}")
    raise SystemExit(1)


def main() -> None:
    if len(sys.argv) < 2:
        _fail("Uso: python scripts/setup_webhook.py https://SEU-DOMINIO")

    base = sys.argv[1].rstrip("/")
    if not base.startswith("https://"):
        _fail("O endereço precisa ser https:// — a Asaas não entrega em http.")

    if not settings.asaas_api_key:
        _fail("ASAAS_API_KEY não está definida. Coloque no .env ou no ambiente.")

    url = base + WEBHOOK_PATH
    sandbox = "sandbox" in settings.asaas_base_url
    ambiente = (
        "SANDBOX (dinheiro de mentira)" if sandbox else "PRODUÇÃO (dinheiro de verdade)"
    )

    print(f"\n  Ambiente: {ambiente}")
    print(f"  API:      {settings.asaas_base_url}")
    print(f"  Webhook:  {url}")
    print(f"  Eventos:  {len(EVENTS)} — {', '.join(EVENTS)}")

    with _client() as client:
        try:
            existing = client.get("/webhooks")
            existing.raise_for_status()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 401:
                _fail("A chave foi recusada (401). Confira ASAAS_API_KEY e o ambiente.")
            _fail(
                f"A Asaas respondeu {error.response.status_code}: {error.response.text[:300]}"
            )
        except httpx.HTTPError as error:
            _fail(f"Não consegui falar com a Asaas: {error}")

        already = next(
            (w for w in existing.json().get("data", []) if w.get("url") == url), None
        )

        # The token is only readable at the moment it is set, so a webhook that
        # already exists gets a fresh one — otherwise the value to put in the
        # environment would be unknowable.
        token = os.getenv("ASAAS_WEBHOOK_TOKEN") or secrets.token_urlsafe(32)

        payload = {
            "name": "Associados CCPA",
            "url": url,
            "email": settings.mail_reply_to,
            "enabled": True,
            "interrupted": False,
            "apiVersion": 3,
            "authToken": token,
            "sendType": "SEQUENTIALLY",
            "events": EVENTS,
        }

        if already:
            print(f"\n  Já existe um webhook para esse endereço (id {already['id']}).")
            if input("  Atualizar com um token novo? [s/N] ").strip().lower() != "s":
                print("  Nada foi alterado.")
                return
            response = client.put(f"/webhooks/{already['id']}", json=payload)
            acao = "atualizado"
        else:
            if (
                not sandbox
                and input("\n  Criar em PRODUÇÃO? [s/N] ").strip().lower() != "s"
            ):
                print("  Nada foi alterado.")
                return
            response = client.post("/webhooks", json=payload)
            acao = "criado"

        if response.status_code >= 400:
            _fail(f"A Asaas recusou ({response.status_code}): {response.text[:400]}")

    print(f"\n  Webhook {acao}. Id: {response.json().get('id')}")
    print("\n  Guarde este token na variável ASAAS_WEBHOOK_TOKEN:\n")
    print(f"      {token}\n")
    print("  Ele não pode ser lido de volta no painel da Asaas. Sem ele no")
    print("  ambiente, a aplicação recusa as notificações e ninguém é ativado.")


if __name__ == "__main__":
    main()

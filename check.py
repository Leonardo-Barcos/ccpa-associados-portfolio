"""Full pre-deploy verification — this project's `npm run build`.

Python is not compiled, so there is no build step that fails when the code is
wrong: a bad import or a `{% endblok %}` in a template only surfaces when
somebody opens the page. This script tries to break the application on
purpose instead: it imports everything, compiles every template, runs the
tests and actually starts the server.

    python check.py

Exits 0 when everything passes and 1 when anything fails, which is what lets
the pre-commit hook and GitHub Actions block a broken deploy.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)

GREEN, RED, YELLOW, GREY, RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[90m",
    "\033[0m",
)
if os.name == "nt":  # enables ANSI colours on older PowerShell
    os.system("")

failures: list[str] = []
warnings: list[str] = []


def heading(text: str) -> None:
    print(f"\n{GREY}{'─' * 62}{RESET}\n  {text}")


def ok(text: str) -> None:
    print(f"  {GREEN}OK{RESET}     {text}")


def fail(stage: str, text: str) -> None:
    print(f"  {RED}FALHOU{RESET} {text}")
    failures.append(stage)


def warn(text: str) -> None:
    print(f"  {YELLOW}AVISO{RESET}  {text}")
    warnings.append(text)


def run(command: list[str]) -> tuple[int, str]:
    process = subprocess.run(command, capture_output=True, text=True)
    return process.returncode, (process.stdout + process.stderr).strip()


# --------------------------------------------------------------------------
# 1. Dependencies installed and mutually compatible
# --------------------------------------------------------------------------
def check_dependencies() -> None:
    heading("1/6  Dependências")
    code, output = run([sys.executable, "-m", "pip", "check"])
    if code == 0:
        ok("nenhum conflito de versão entre os pacotes")
    else:
        fail("dependências", "pip check reclamou:")
        print(f"{GREY}{output}{RESET}")


# --------------------------------------------------------------------------
# 2. Static analysis — catches what a compiler would: undefined names, dead
#    imports, unused variables, f-strings with no placeholder
# --------------------------------------------------------------------------
def check_lint() -> None:
    heading("2/6  Análise estática (ruff)")
    code, output = run([sys.executable, "-m", "ruff", "check", "."])
    if "No module named" in output:
        warn("ruff não instalado — rode: pip install -r requirements-dev.txt")
        return
    if code == 0:
        ok("nenhum problema encontrado no código")
    else:
        fail("lint", "o ruff encontrou problemas:")
        print(f"{GREY}{output}{RESET}")
        print(f"{GREY}  (muitos deles se corrigem com: ruff check --fix .){RESET}")

    code, output = run([sys.executable, "-m", "ruff", "format", "--check", "."])
    if code == 0:
        ok("formatação consistente")
    else:
        warn("arquivos fora do padrão de formatação — rode: ruff format .")


# --------------------------------------------------------------------------
# 3. Templates — a Jinja error only fires when the page is opened, so every
#    template gets compiled here
# --------------------------------------------------------------------------
def check_templates() -> None:
    heading("3/6  Templates")
    from jinja2 import TemplateSyntaxError

    # The REAL application environment, with our filters and globals
    # registered — a bare one would report our own filters as missing.
    from app.templating import templates

    folder = ROOT / "app" / "templates"
    env = templates.env
    files = sorted(folder.rglob("*.html"))

    if not files:
        fail("templates", "nenhum template encontrado em app/templates")
        return

    broken = 0
    for path in files:
        name = path.relative_to(folder).as_posix()
        try:
            env.get_template(name)
        except TemplateSyntaxError as error:
            fail("templates", f"{name}, linha {error.lineno}: {error.message}")
            broken += 1
        except Exception as error:  # noqa: BLE001
            fail("templates", f"{name}: {error}")
            broken += 1

    if not broken:
        ok(f"{len(files)} templates compilam sem erro de sintaxe")

    # Compiling does not open {% extends %}, {% include %} or {% from %}: a
    # template that imports a missing file compiles fine and only fails when
    # the page is opened. Every reference is resolved here instead.
    from jinja2 import meta

    missing = []
    for path in files:
        name = path.relative_to(folder).as_posix()
        source = path.read_text(encoding="utf-8")
        for ref in meta.find_referenced_templates(env.parse(source)):
            if ref and not (folder / ref).exists():
                missing.append(f"{name} → {ref}")
    if missing:
        for item in missing:
            fail("templates", f"template inexistente: {item}")
    else:
        ok("todo extends/include/import aponta para um arquivo que existe")


# --------------------------------------------------------------------------
# 4. Configuration — every Settings field must be documented in .env.example,
#    otherwise somebody deploys without knowing a key was missing
# --------------------------------------------------------------------------
def check_configuration() -> None:
    heading("4/6  Configuração")
    from app.config import Settings, settings

    example = ROOT / ".env.example"
    if not example.exists():
        fail("configuração", ".env.example não existe")
        return

    text = example.read_text(encoding="utf-8").upper()
    missing = [f.upper() for f in Settings.model_fields if f.upper() not in text]

    if missing:
        warn(".env.example não documenta: " + ", ".join(sorted(missing)))
    else:
        ok(f"as {len(Settings.model_fields)} variáveis estão no .env.example")

    if settings.is_prod:
        if not settings.resend_api_key:
            # Aviso, e não falha: dá para operar sem e-mail, e a página de
            # sucesso se adapta para entregar o link ela mesma. O que não dá é
            # subir sem saber que se está fazendo isso.
            warn(
                "RESEND_API_KEY vazia — nenhum e-mail sai. O link de pagamento "
                "vai existir só na página de sucesso, e quem fechar a aba "
                "antes de pagar fica sem forma de voltar"
            )
        if settings.payment_provider == "mock":
            fail("configuração", "PAYMENT_PROVIDER=mock em produção")
        if settings.payment_provider == "asaas":
            if not settings.asaas_api_key:
                fail("configuração", "ASAAS_API_KEY vazia em produção")
            if not settings.asaas_webhook_token:
                # Sem token, qualquer requisição ao webhook ativa uma
                # associação sem que ninguém tenha pagado.
                fail("configuração", "ASAAS_WEBHOOK_TOKEN vazio em produção")
            if "sandbox" in settings.asaas_base_url:
                fail("configuração", "ASAAS_BASE_URL ainda aponta para o sandbox")
        if len(settings.secret_key) < 32:
            # Assina a sessão do painel. Vazia, cada instância serverless
            # inventaria a sua e derrubaria o login a cada requisição; curta,
            # dá para adivinhar e entrar como qualquer usuário.
            fail(
                "configuração",
                "SECRET_KEY vazia ou com menos de 32 caracteres em produção",
            )
        if settings.base_url.startswith("http://"):
            fail("configuração", "BASE_URL sem HTTPS em produção")
        if "sqlite" in settings.database_url:
            # Em serverless o disco é efêmero: o SQLite some junto com a
            # instância, levando os associados cadastrados.
            fail("configuração", "produção com SQLite — use PostgreSQL")
        if settings.auto_create_tables:
            warn(
                "AUTO_CREATE_TABLES=true em produção — rode "
                "scripts/init_db.py uma vez e deixe false"
            )
        if not failures:
            ok("configuração de produção coerente")
    else:
        ok("rodando em modo desenvolvimento (checagens de produção puladas)")


# --------------------------------------------------------------------------
# 5. Test suite
# --------------------------------------------------------------------------
def check_tests() -> None:
    heading("5/6  Testes automatizados")
    code, output = run([sys.executable, "-m", "pytest", "-q"])
    last_line = output.strip().splitlines()[-1] if output.strip() else ""
    if code == 0:
        ok(last_line or "todos os testes passaram")
    else:
        fail("testes", "algum teste falhou:")
        # Only the report's tail, and no line longer than a screen: one test
        # with a huge parameter once filled the whole terminal with \x00.
        lines = [
            line if len(line) <= 300 else line[:300] + " [...]"
            for line in output.strip().splitlines()
        ]
        failed = [line for line in lines if line.startswith(("FAILED", "ERROR"))]
        print(f"{GREY}" + "\n".join(lines[-40:]) + f"{RESET}")
        if failed:
            print("\n  Testes que falharam:")
            for line in failed:
                print(f"    {line}")


# --------------------------------------------------------------------------
# 6. Smoke test — start the real app and hit the real routes. This is what
#    catches failures that only exist at runtime.
# --------------------------------------------------------------------------
def check_smoke() -> None:
    heading("6/6  A aplicação sobe e responde")
    os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///./check.db")

    import logging

    for noisy in ("httpx", "asyncio", "ccpa.email"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        from fastapi.testclient import TestClient

        from app.main import app
    except Exception as error:  # noqa: BLE001
        fail("fumaça", f"a aplicação nem importa: {error}")
        return

    routes = [
        ("/healthz", 200),
        ("/", 307),  # redirects to the form
        ("/associe-se", 200),
        ("/associe-se?tipo=pj", 200),
        ("/associe-se/sucesso/nao-existe", 302),
        ("/rota-que-nao-existe", 307),
        # The panel must never answer without a session.
        ("/painel", 303),
        ("/painel/associados", 303),
        ("/painel/associados/exportar.csv", 303),
        ("/painel/entrar", 200),
        ("/painel/marcas", 303),
        ("/marcas/999", 404),
    ]

    started = time.time()
    try:
        with TestClient(app, follow_redirects=False) as client:
            for path, expected in routes:
                status = client.get(path).status_code
                if status != expected:
                    fail("fumaça", f"GET {path} devolveu {status}, esperado {expected}")
                else:
                    ok(f"GET {path} → {expected}")

            signup = client.get("/associe-se").text
            for marker, description in (
                ('action="/associe-se/pf"', "formulário de pessoa física"),
                ('action="/associe-se/pj"', "formulário de empresa"),
                ("/static/css/ccpa.css", "folha de estilo"),
                ("/static/js/form.js", "script do formulário"),
                ("/static/img/logo-ccpa.png", "logo"),
            ):
                if marker not in signup:
                    fail("fumaça", f"a página não traz {description}")

            for marker, description in (
                ("Clube de Criativos do Pará", "nome da entidade"),
                ("cartão de crédito", "aviso de pagamento só no cartão"),
            ):
                if marker not in signup:
                    fail("fumaça", f"a página não traz {description}")

            for asset in (
                "app/static/css/ccpa.css",
                "app/static/js/form.js",
                "app/static/img/logo-ccpa.png",
                "app/static/img/logo-ccpa-branca.png",
                "app/static/img/favicon.ico",
                "app/static/img/favicon-32.png",
                "app/static/img/apple-touch-icon.png",
                "app/static/css/painel.css",
                "app/static/js/painel.js",
            ):
                if not (ROOT / asset).exists():
                    fail("fumaça", f"arquivo estático ausente: {asset}")

            if not (ROOT / "app/static/termos-de-adesao.pdf").exists():
                warn(
                    "app/static/termos-de-adesao.pdf ainda não existe — "
                    "o link dos termos vai dar 404"
                )
    except Exception as error:  # noqa: BLE001
        fail("fumaça", f"erro ao rodar a aplicação: {error}")
    finally:
        (ROOT / "check.db").unlink(missing_ok=True)

    print(f"{GREY}         ({time.time() - started:.1f}s){RESET}")


# --------------------------------------------------------------------------
def main() -> int:
    print(f"\n{GREY}  Verificando o projeto CCPA{RESET}")

    check_dependencies()
    check_lint()
    check_templates()
    check_configuration()
    check_tests()
    check_smoke()

    print(f"\n{GREY}{'─' * 62}{RESET}")
    if warnings:
        print(f"  {YELLOW}{len(warnings)} aviso(s){RESET} — não impedem o deploy.")
    if failures:
        stages = ", ".join(dict.fromkeys(failures))
        print(f"  {RED}REPROVADO{RESET} em: {stages}")
        print("  Corrija antes de subir.\n")
        return 1

    print(f"  {GREEN}APROVADO{RESET} — pode subir.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

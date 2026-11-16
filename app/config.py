"""Application settings, loaded from the environment (12-factor)."""

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "Clube de Criativos do Pará"
    app_env: Literal["development", "production"] = "development"
    base_url: str = "http://localhost:8000"

    database_url: str = "sqlite+pysqlite:///./ccpa.db"

    # Creating the tables on every boot is fine on SQLite and wasteful on a
    # serverless platform, where a cold start would run it again on each new
    # instance. In production the tables are created once, by
    # `python scripts/init_db.py`, and this is switched off.
    auto_create_tables: bool = True

    # Resend
    resend_api_key: str = ""
    mail_from: str = "CCPA <associados@ccpara.com.br>"
    mail_reply_to: str = "criativosdopara@gmail.com"
    mail_bcc_admin: str = ""

    # Payments. The default is `mock` so the application runs out of the box
    # with no gateway account; `check.py` refuses to pass with `mock` in
    # production, which is what stops it reaching the server by accident.
    payment_provider: Literal["mock", "asaas"] = "mock"

    asaas_api_key: str = ""
    asaas_base_url: str = "https://api.asaas.com/v3"
    asaas_webhook_token: str = ""

    # Pix Automático (recurring debit authorised once in the payer's bank
    # app). Off until Asaas enables it on the account: it requires a company
    # account, a CNPJ active for six months and no pending registration. With
    # this off, the option simply does not appear on the form.
    pix_automatic_enabled: bool = False

    # Signs the management panel's session cookie. Anyone holding it can sign
    # in as any panel user, so it is a secret like the Asaas key. Generate with
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    secret_key: str = ""

    @field_validator("database_url")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Point a bare Postgres URL at the driver the project installs.

        Hosted Postgres providers hand out `postgres://` or `postgresql://`
        URLs. SQLAlchemy reads the scheme as the driver name and would reach
        for psycopg2, which is not installed — a confusing crash at boot for
        a URL that was copied correctly.
        """
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @property
    def is_prod(self) -> bool:
        return self.app_env == "production"

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def email_enabled(self) -> bool:
        """Whether the application can actually deliver e-mail.

        It is allowed to run without it, and the signup still works — but the
        payment link then exists in exactly one place, the confirmation page.
        Everything the member reads has to say so, instead of telling them to
        check an inbox where nothing will arrive.
        """
        return bool(self.resend_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

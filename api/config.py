"""Einstellungen aus der Umgebung. Keine Werte hier hartkodieren."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://maex:change_me@db:5432/maex_agent"
    agent_api_token: str = "change_me_long_random"

    tenant_name: str = "Pilotbetrieb"
    tenant_timezone: str = "Europe/Berlin"
    team_phone: str = "+49000000000"
    max_call_seconds: int = 420

    # Kalter Pfad: n8n und Dispatcher (docs/03 §outbox, docs/11 §events)
    n8n_webhook_url: str = "http://n8n:5678/webhook/maex"
    n8n_basic_auth_user: str = ""
    n8n_basic_auth_password: str = ""
    n8n_timeout_seconds: float = 10.0
    dispatcher_interval_seconds: float = 5.0
    dispatcher_batch: int = 20
    # Druckbruecke im Lokal (T-4.6): leer heisst, /v1/kitchen/* ist zu.
    kitchen_bridge_token: str = ""
    # Betrieb, fuer den das Token gilt. Gesetzt, ist jeder andere tenant_id
    # abgelehnt: ein geleaktes Token eines Lokals oeffnet nicht die Bons aller.
    kitchen_bridge_tenant_id: str = ""

    menu_fuzzy_threshold_high: float = 0.72
    menu_fuzzy_threshold_low: float = 0.45

    # Anrufprotokoll (docs/17): Loeschfrist der CSV in Tagen, D10
    call_log_retention_days: int | None = None


settings = Settings()

"""Einstellungen aus der Umgebung. Keine Werte hier hartkodieren."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://maex:change_me@db:5432/maex_agent"
    agent_api_token: str = "change_me_long_random"

    tenant_name: str = "Yoki Yoki GmbH"
    tenant_timezone: str = "Europe/Berlin"
    team_phone: str = "+49000000000"
    max_call_seconds: int = 420

    menu_fuzzy_threshold_high: float = 0.72
    menu_fuzzy_threshold_low: float = 0.45


settings = Settings()

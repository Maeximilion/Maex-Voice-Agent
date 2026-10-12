"""Einstellungen aus der Umgebung. Keine Werte hier hartkodieren."""

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value `.env.example` ships, readable by everybody: no secret.
PLACEHOLDER_TOKEN = "change_me_long_random"
# Where the placeholder may open the agent API: a workbench and CI. Any other
# `ENV` counts as production, a typo ("production", "Prod") included, so a
# server never runs on the placeholder because of one.
WORKBENCH_ENVS = frozenset({"dev", "test"})


def token_usable(token: str, env: str) -> bool:
    """May this agent token open anything? A blank one never, the placeholder
    only on a workbench. One rule for the settings, which refuse to load with
    another one outside a workbench, and for `core/auth.py`, which does not
    rely on that."""
    token = token.strip()
    return bool(token) and (token != PLACEHOLDER_TOKEN or env in WORKBENCH_ENVS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://maex:change_me@db:5432/maex_agent"
    # Checked also when it is not set: the default is the placeholder.
    agent_api_token: str = Field(default=PLACEHOLDER_TOKEN, validate_default=True)

    @field_validator("agent_api_token")
    @classmethod
    def _no_open_door_outside_a_workbench(cls, token: str, info: ValidationInfo) -> str:
        """A server with a token anybody can know does not start. On the field
        and not on the model: the error then shows this value only, never the
        other settings, which hold passwords."""
        env = info.data.get("env", "")
        if env not in WORKBENCH_ENVS and not token_usable(token, env):
            raise ValueError(
                "AGENT_API_TOKEN is missing, blank or still the placeholder of "
                f".env.example, and ENV is '{env}': set a long random value "
                "(`openssl rand -hex 32`). Only ENV=dev and ENV=test run on "
                "the placeholder"
            )
        return token

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
    # Betrieb, fuer den das Token gilt; ohne ihn ist /v1/kitchen/* zu. Jeder
    # andere tenant_id wird abgelehnt: ein Token oeffnet nie die Bons aller.
    kitchen_bridge_tenant_id: str = ""

    # Language model of the conversation core (T-2.4, docs/13 §0). Empty means
    # no model is configured: text phone and evals run on the scripted stand-in.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = 15.0
    # Upper limit for one answer. A turn is a sentence or a tool call; a model
    # that reasons before it answers needs more.
    llm_max_output_tokens: int = 600
    # Sent as `reasoning_effort` when set. A reasoning model needs "none", or it
    # thinks until the output limit and answers nothing (docs/18 §5). Empty:
    # the parameter is not sent.
    llm_reasoning_effort: str = ""
    # Price of the model in cents per million tokens. Without both, the cost
    # of a call is unknown and stays empty; 0 is a price (a local model).
    llm_input_cents_per_mtok: int | None = Field(default=None, ge=0)
    llm_output_cents_per_mtok: int | None = Field(default=None, ge=0)

    menu_fuzzy_threshold_high: float = 0.72
    menu_fuzzy_threshold_low: float = 0.45

    # Anrufprotokoll (docs/17): Loeschfrist der CSV in Tagen, D10
    call_log_retention_days: int | None = None


settings = Settings()

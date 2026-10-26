"""Einstellungen aus der Umgebung. Keine Werte hier hartkodieren."""

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The value .env.example ships. Public, so in production it is no token at all.
TOKEN_PLACEHOLDER = "change_me_long_random"
# 32 characters is what `openssl rand -hex 16` prints.
TOKEN_MIN_LENGTH = 32
# Where a short or known token is fine. Every other value of ENV counts as
# production, a misspelled one included.
LOCAL_ENVS = frozenset({"dev", "test"})


def valid_production_token(token: str) -> bool:
    """Whitespace cannot turn a public or short value into a usable token."""
    return (
        token != TOKEN_PLACEHOLDER
        and len(token) >= TOKEN_MIN_LENGTH
        and not any(character.isspace() for character in token)
    )


class Settings(BaseSettings):
    # hide_input_in_errors: a value that fails validation is a secret or sits next
    # to one, and the error goes to the log.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    env: str = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://maex:change_me@db:5432/maex_agent"
    agent_api_token: str = TOKEN_PLACEHOLDER
    # Shared by Caddy and the API: outside dev and test the operations view
    # answers only requests that carry it, i.e. came through Caddy and its
    # Basic-Auth (api/core/auth.py, docs/13 §3). Empty closes the view there.
    gui_proxy_secret: str = ""

    tenant_name: str = "Pilotbetrieb"
    tenant_timezone: str = "Europe/Berlin"
    team_phone: str = "+49000000000"
    max_call_seconds: int = 420
    # Written into a new tenant by the seed (service_config.privacy_notice_address,
    # docs/09 L4). Empty: none, and the greeting leaves the sentence out.
    privacy_notice_address: str = ""

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

    # Service `voice`, the own voice layer (docs/20 §7). It serves the tenant of
    # `tenant_name`. The telephone system presents the token as the password of Basic
    # credentials on the media connection and when the dialplan asks how a call
    # ended; without one `voice` does not start.
    voice_media_token: str = ""
    # The number the AI telephone answers (E.164). `voice` does not start when
    # the team number is this one: the team would ring us (docs/20 §5).
    voice_number: str = ""
    # Seconds of silence after a sentence before the code asks "Sind Sie noch
    # da?" (docs/20 §4). An assumption until real calls say otherwise.
    voice_silence_seconds: float = Field(default=8.0, gt=0)
    # Ear and mouth by the names of their engines in `telephony/app.py`
    # (`EARS`, `MOUTHS`), the local engines of the test phase (docs/20 §6).
    # `fake` is the drill pair of the workbench, an ear that hears nothing and
    # a mouth of noise, and is refused outside dev and test.
    voice_ear: str = ""
    voice_mouth: str = ""
    # Where the ear's engine runs: on the graphics card, or on the processor,
    # where it takes seconds for a sentence (docs/20 §11).
    voice_ear_device: Literal["cuda", "cpu"] = "cuda"
    # The mouth's voice, by the name of its file among the model files; empty
    # for the engine's own (`telephony/adapters/`). Which one is Maxi's to
    # hear (D4).
    voice_mouth_voice: str = ""

    menu_fuzzy_threshold_high: float = 0.72
    menu_fuzzy_threshold_low: float = 0.45

    # Anrufprotokoll (docs/17): Loeschfrist der CSV in Tagen, D10
    call_log_retention_days: int | None = None

    @model_validator(mode="after")
    def _production_needs_real_tokens(self) -> Self:
        """Refuse to start outside dev and test on a token anyone can know.

        The agent token is all that stands between the network and the write
        tools. A server set up from .env.example with the line left as it is,
        or left out, would otherwise run on a value printed in this repository.
        The kitchen token is offered to the internet by the proxy
        (/v1/kitchen/*) and gets the same rule when it is set (security audit
        of 15.10.2026, #252 finding 5); the n8n password is checked by the
        dispatcher, the one process that has it (api/events/dispatcher.py). The values themselves stay out of the messages: they end
        up in a log.
        """
        if self.env in LOCAL_ENVS:
            return self
        rule = (
            f"is the placeholder, contains whitespace, or is shorter than "
            f"{TOKEN_MIN_LENGTH} characters; set a random one in .env "
            "(openssl rand -hex 32)"
        )
        if not valid_production_token(self.agent_api_token):
            raise ValueError(f"AGENT_API_TOKEN {rule}")
        kitchen = self.kitchen_bridge_token
        if kitchen and not valid_production_token(kitchen):
            raise ValueError(f"KITCHEN_BRIDGE_TOKEN {rule}")
        if kitchen == self.agent_api_token:
            raise ValueError("KITCHEN_BRIDGE_TOKEN must differ from AGENT_API_TOKEN")
        return self


settings = Settings()

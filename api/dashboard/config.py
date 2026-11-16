"""Settings of the operator dashboard, from its own stack's environment."""

from typing import Self
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from api.core.tokens import LOCAL_ENVS, TOKEN_MIN_LENGTH, valid_production_token


class DashboardSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    env: str = "dev"
    log_level: str = "INFO"
    # A name of its own, so a customer stack's DATABASE_URL is never taken by
    # mistake: the operator's accounts live outside every customer's database.
    operator_database_url: str = (
        "postgresql+psycopg://maex:change_me@db:5432/maex_operator"
    )
    # Set by Caddy on every request to ops.<domain> (as GUI_PROXY_SECRET is for
    # a customer's site): every customer's api shares a network with the
    # dashboard (docs/23 §8) and must not reach its sign-in past the proxy.
    operator_proxy_secret: str = ""
    # Where the operator's browser reaches the dashboard, `https://ops.<domain>`.
    # A passkey is bound to this origin and to its host name (the relying party
    # id); both come from here, never from a request's Host header, which the
    # sender chooses (docs/23 §5).
    operator_origin: str = "http://localhost:8002"
    # The push server and the token that may publish to its `ops` topic. Empty
    # until the operator stack has its Compose file (#571): a push is then
    # only logged (`dashboard/push.py`).
    operator_push_url: str = ""
    operator_push_token: str = ""

    @property
    def relying_party_id(self) -> str:
        return urlsplit(self.operator_origin).hostname or ""

    @model_validator(mode="after")
    def _production_needs_the_proxy_secret(self) -> Self:
        if self.env not in LOCAL_ENVS and not valid_production_token(
            self.operator_proxy_secret
        ):
            raise ValueError(
                "OPERATOR_PROXY_SECRET is missing, the placeholder, contains "
                f"whitespace, or is shorter than {TOKEN_MIN_LENGTH} characters"
            )
        return self

    @model_validator(mode="after")
    def _the_origin_is_an_origin(self) -> Self:
        local = self.env in LOCAL_ENVS
        if not local and "operator_origin" not in self.model_fields_set:
            raise ValueError("OPERATOR_ORIGIN is missing: https://ops.<domain>")
        try:
            parts = urlsplit(self.operator_origin)
            port = f":{parts.port}" if parts.port else ""
            # Scheme, host and port and nothing else, as a browser writes it.
            fine = (
                bool(parts.hostname)
                and self.operator_origin == f"{parts.scheme}://{parts.hostname}{port}"
                and parts.scheme in (("http", "https") if local else ("https",))
            )
        except ValueError:
            fine = False
        if not fine:
            raise ValueError(
                "OPERATOR_ORIGIN must be an origin like https://ops.example.com: "
                "https outside dev and test, lower case, no path, no trailing slash"
            )
        return self


settings = DashboardSettings()

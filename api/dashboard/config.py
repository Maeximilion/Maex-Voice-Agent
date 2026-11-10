"""Settings of the operator dashboard, from its own stack's environment."""

from typing import Self

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


settings = DashboardSettings()

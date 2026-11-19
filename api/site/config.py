"""Settings of the public site, from its own environment (docs/13 §9).

Every value is optional. A value that is not set switches its part of the page
off instead of showing a link that leads nowhere: no sign-in target, no
"Kunden-Login" link; no contact address, no mail link; no inquiry route, a
form that cannot be sent.
"""

import re
from typing import Self
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# One address of the usual shape; the mail service that delivers inquiries
# validates again. No quoted local parts, no display names.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")


class SiteSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    env: str = "dev"
    log_level: str = "INFO"
    # Where "Kunden-Login" leads: the sign-in of the first customer stack, an
    # `https://` address or a path of this host. Empty: the entry is shown as
    # not available yet.
    site_login_url: str = ""
    # Where interested restaurants may write besides the form. Empty: no link.
    site_contact_email: str = ""
    # The path the inquiry form posts to, once a route, a mail service, a target
    # address and the privacy text exist (#892). Empty: the form is shown, but
    # cannot be sent.
    site_inquiry_action: str = ""

    @model_validator(mode="after")
    def _values_are_what_they_say(self) -> Self:
        login = self.site_login_url
        if login:
            parts = urlsplit(login)
            web = (
                parts.scheme == "https" and bool(parts.hostname) and not parts.username
            )
            local = self.env in ("dev", "test") and parts.scheme == "http"
            path = login.startswith("/") and not login.startswith("//")
            if not (web or path or (local and parts.hostname)):
                raise ValueError("SITE_LOGIN_URL must be an https:// address or a path")
        mail = self.site_contact_email
        if mail and not EMAIL.fullmatch(mail):
            raise ValueError("SITE_CONTACT_EMAIL must be one e-mail address")
        action = self.site_inquiry_action
        if action and not (action.startswith("/") and not action.startswith("//")):
            raise ValueError("SITE_INQUIRY_ACTION must be a path of this site")
        return self

"""Time-based one-time codes (RFC 6238) for the operator login, standard
library only (docs/23 §5, §10): HMAC-SHA1, 30-second steps, six digits, what
every authenticator app reads from an `otpauth://` link.

A code is taken from the step before, the current one and the one after, so a
phone clock off by up to 30 seconds still signs in. The step of the last code
used is kept on the account; a code of that step or an older one is refused,
so a code seen over someone's shoulder opens nothing a second time.
"""

import base64
import hmac
import re
import secrets
from urllib.parse import quote, urlencode

STEP_SECONDS = 30
DIGITS = 6
SECRET_BYTES = 20
ISSUER = "Maex Operator"
CODE = re.compile(r"[0-9]{6}")


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode()


def code_at(secret: str, step: int, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP of `step`, the counter TOTP derives from the time."""
    mac = hmac.digest(base64.b32decode(secret), step.to_bytes(8, "big"), "sha1")
    offset = mac[-1] & 0x0F
    value = int.from_bytes(mac[offset : offset + 4], "big") & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def matching_step(
    secret: str, code: str, now: float, last_step: int | None
) -> int | None:
    """The step `code` belongs to, or None when it opens nothing. `now` is
    seconds since the epoch."""
    code = code.replace(" ", "")
    if not CODE.fullmatch(code) or not code.isascii():
        return None
    current = int(now) // STEP_SECONDS
    for step in (current - 1, current, current + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def uri(secret: str, account: str) -> str:
    """The link an authenticator app takes (Key Uri Format)."""
    label = quote(f"{ISSUER}:{account}", safe=":@")
    query = urlencode({"secret": secret, "issuer": ISSUER}, quote_via=quote)
    return f"otpauth://totp/{label}?{query}"

"""The rule every token and secret of a stack meets outside dev and test.

Apart from `api/config.py` so the operator dashboard (`api/dashboard/`) can use
it: importing `api.config` builds the customer stack's settings, which refuse
to start without that stack's tokens.
"""

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

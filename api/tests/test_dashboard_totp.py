"""TOTP of the operator login (RFC 6238, docs/23 §5): the RFC's own test values,
the clock window, and that a code is never taken twice."""

import base64
from urllib.parse import parse_qs, urlsplit

from api.dashboard import totp

# RFC 6238 appendix B, HMAC-SHA1, eight digits.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
RFC_VALUES = {
    59: "94287082",
    1111111109: "07081804",
    1111111111: "14050471",
    1234567890: "89005924",
    2000000000: "69279037",
    20000000000: "65353130",
}


def test_codes_match_the_rfc_test_values():
    for seconds, code in RFC_VALUES.items():
        assert totp.code_at(RFC_SECRET, seconds // totp.STEP_SECONDS, digits=8) == code


def test_six_digits_are_the_last_six_of_the_rfc_value():
    assert totp.code_at(RFC_SECRET, 59 // totp.STEP_SECONDS) == "287082"


def test_a_code_of_the_step_before_or_after_is_taken():
    secret = totp.new_secret()
    step = 1_000_000
    now = step * totp.STEP_SECONDS
    for near in (step - 1, step, step + 1):
        assert totp.matching_step(secret, totp.code_at(secret, near), now, None) == near


def test_a_code_two_steps_away_is_refused():
    secret = totp.new_secret()
    step = 1_000_000
    now = step * totp.STEP_SECONDS
    for far in (step - 2, step + 2):
        assert totp.matching_step(secret, totp.code_at(secret, far), now, None) is None


def test_a_code_already_used_or_older_is_refused():
    secret = totp.new_secret()
    step = 1_000_000
    now = step * totp.STEP_SECONDS
    code = totp.code_at(secret, step)
    assert totp.matching_step(secret, code, now, step) is None
    # Nor the step before, after the current one was used.
    older = totp.code_at(secret, step - 1)
    assert totp.matching_step(secret, older, now, step) is None
    assert totp.matching_step(secret, totp.code_at(secret, step + 1), now, step) == (
        step + 1
    )


def test_spaces_in_the_code_are_ignored_and_other_input_refused():
    secret = totp.new_secret()
    now = 1_000_000 * totp.STEP_SECONDS
    code = totp.code_at(secret, 1_000_000)
    assert totp.matching_step(secret, f"{code[:3]} {code[3:]}", now, None) is not None
    for wrong in ("", "12345", "1234567", "abcdef", "١٢٣٤٥٦"):
        assert totp.matching_step(secret, wrong, now, None) is None


def test_the_secret_has_160_bits_and_the_uri_names_issuer_and_account():
    secret = totp.new_secret()
    assert len(base64.b32decode(secret)) == 20
    uri = urlsplit(totp.uri(secret, "maxi"))
    assert uri.scheme == "otpauth" and uri.netloc == "totp"
    assert uri.path == "/Maex%20Operator:maxi"
    query = parse_qs(uri.query)
    assert query["secret"] == [secret]
    assert query["issuer"] == ["Maex Operator"]

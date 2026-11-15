"""Probe (review #207, area G): a replay of the just-used TOTP code from another
address, without the operator's device cookie, is audited `repeat` (no alarm)
although it is the takeover case `factor` was meant to flag."""

from sqlalchemy import select

from api.dashboard import access
from api.dashboard.models import OperatorAudit
from api.tests.test_dashboard_login import (  # noqa: F401
    PASSWORD,
    _code,
    clock,
    factors,
    operator_url,
    sessions,
)


def test_replay_from_another_address_is_still_a_factor_alarm(sessions, factors, clock):
    code = _code(factors.totp_secret, clock.at)
    with sessions() as db:
        signed = access.sign_in(db, "maxi", PASSWORD, code, "198.51.100.7", clock.at)
    assert signed.token
    # An attacker who knows the password and saw the code, elsewhere, no device cookie.
    with sessions() as db:
        try:
            access.sign_in(db, "maxi", PASSWORD, code, "203.0.113.9", clock.at)
        except access.SignInRefused:
            pass
    with sessions() as db:
        reasons = [
            (row.detail["reason"], row.detail["address"])
            for row in db.scalars(
                select(OperatorAudit).where(
                    OperatorAudit.action == "operator.sign_in_failed"
                )
            )
        ]
    assert reasons == [("factor", "203.0.113.9")], reasons

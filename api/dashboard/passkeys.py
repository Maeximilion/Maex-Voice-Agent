"""Passkeys of the operator (WebAuthn, docs/23 §5): the normal second factor.

A passkey signs a random challenge together with the origin of the page that
asked, so a look-alike site gets a signature that is worth nothing here. What
this module holds to:

- The relying party id and the origin come from the settings
  (`OPERATOR_ORIGIN`), never from a request.
- A challenge is 32 random bytes, kept in the database for one holder and one
  purpose: the session for a registration or a step-up, the sign-in attempt's
  cookie for a sign-in. It is taken out before its answer is looked at, so it
  is never checked twice, and it is worthless after five minutes. Only the
  challenge the answer names is taken: an older tab's answer leaves the newer
  tab's challenge in place (`signed_challenge`). Nobody's
  challenge is ever removed to make room for another's: anyone may ask for a
  sign-in challenge, and a flood must not take the operator's away. One client
  address holds at most `SIGN_IN_PER_ADDRESS` live sign-in challenges; more
  are refused (#827 F1).
- An answer is input from outside until it is verified. Whatever its parsing
  or its check raises is a refusal, never a server error: at the sign-in that
  runs after the password check, and an error there would tell that the
  password was right.
- The operator is verified on the device (PIN, finger, face) at every use, and
  the passkey is stored on it (discoverable), so the sign-in page hands out no
  list of credentials to someone who only knows a name.
- A signature counter that does not grow means a copy of the passkey: refused,
  and named in the audit.

No FastAPI. `access.py` decides when a passkey counts; this module only says
whether the answer of a ceremony holds.
"""

import json
import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    options_to_json,
    parse_authentication_credential_json,
    parse_client_data_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from api.dashboard.config import settings
from api.dashboard.models import (
    OperatorAccount,
    OperatorChallenge,
    OperatorPasskey,
    OperatorSession,
)
from api.domain.access.hashing import token_hash

CHALLENGE_LIFE = timedelta(minutes=5)
CHALLENGE_BYTES = 32
# Live sign-in challenges of one client address. A person has a tab or two; a
# global cap instead would let a flood shut out the operator's passkey.
SIGN_IN_PER_ADDRESS = 10
# The browser's own limit for one ceremony, in milliseconds.
TIMEOUT_MS = 120_000
# Characters of an answer. A device's is one to four thousand; nothing
# longer is parsed.
ANSWER_MAX = 32_768
PASSKEYS_MAX = 10
NAME_MAX = 40
RP_NAME = "Maex Operator"
REGISTER, SIGN_IN, STEP_UP = "register", "sign-in", "step-up"


class Refused(Exception):
    """The answer of a ceremony does not hold. `reason` is for the audit only:
    the operator's browser gets the same answer for every refusal."""

    def __init__(self, reason: str = "factor"):
        super().__init__(reason)
        self.reason = reason


def for_session(session_id: uuid.UUID) -> str:
    return f"session:{session_id}"


def for_attempt(token: str) -> str:
    return f"attempt:{token_hash(token)}"


class TooMany(Exception):
    """This address holds `SIGN_IN_PER_ADDRESS` live sign-in challenges."""


def issue(
    db: Session,
    bound_to: str,
    purpose: str,
    now: datetime,
    client_address: str | None = None,
) -> bytes:
    """A new challenge for this holder and purpose, in place of the one it
    had. Commits. With `client_address` (a sign-in) raises `TooMany` when that
    address holds the cap of live challenges of other holders already: a
    flood is refused, nobody's challenge is taken away (#827 F1)."""
    db.execute(delete(OperatorChallenge).where(OperatorChallenge.expires_at <= now))
    if client_address is not None:
        # One address at a time, so parallel asks cannot all pass the count.
        db.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(client_address)))
        )
        live = db.scalar(
            select(func.count())
            .select_from(OperatorChallenge)
            .where(
                OperatorChallenge.client_address == client_address,
                OperatorChallenge.bound_to != bound_to,
            )
        )
        if (live or 0) >= SIGN_IN_PER_ADDRESS:
            db.rollback()
            raise TooMany
    challenge = secrets.token_bytes(CHALLENGE_BYTES)
    expires = now + CHALLENGE_LIFE
    db.execute(
        insert(OperatorChallenge)
        .values(
            bound_to=bound_to,
            purpose=purpose,
            challenge=challenge,
            expires_at=expires,
            client_address=client_address,
        )
        .on_conflict_do_update(
            constraint="uq_operator_challenge_holder",
            set_={
                "challenge": challenge,
                "expires_at": expires,
                "client_address": client_address,
            },
        )
    )
    db.commit()
    return challenge


def take(
    db: Session,
    bound_to: str,
    purpose: str,
    now: datetime,
    expected: bytes | None = None,
) -> bytes | None:
    """The live challenge of this holder, taken out for good: whatever the
    answer turns out to be, nothing is checked against it a second time.
    With `expected` only that very value is taken (a form's token, which the
    browser sends back as it is): another value takes nothing, so an older
    tab's form does not spend the newer tab's token. Commits."""
    wanted = [
        OperatorChallenge.bound_to == bound_to,
        OperatorChallenge.purpose == purpose,
    ]
    if expected is not None:
        wanted.append(OperatorChallenge.challenge == expected)
    row = db.execute(
        delete(OperatorChallenge)
        .where(*wanted)
        .returning(OperatorChallenge.challenge, OperatorChallenge.expires_at)
    ).first()
    db.commit()
    if row is None or row.expires_at <= now:
        return None
    return bytes(row.challenge)


def signed_challenge(answer: str) -> bytes:
    """The challenge an answer says it signed, for `take(expected=...)`: an
    older tab's answer then spends nothing of the newer tab's (#816). Empty,
    which matches no challenge, when the answer cannot be read; never raises
    (module docstring). Whether the answer holds is `verify`'s question."""
    if len(answer) > ANSWER_MAX:
        return b""
    try:
        sent = json.loads(answer)["response"]["clientDataJSON"]
        return parse_client_data_json(base64url_to_bytes(sent)).challenge
    except Exception:  # noqa: BLE001  any error is a refusal (module docstring)
        return b""


def of_account(db: Session, account_id: uuid.UUID) -> list[OperatorPasskey]:
    return list(
        db.scalars(
            select(OperatorPasskey)
            .where(OperatorPasskey.account_id == account_id)
            .order_by(OperatorPasskey.created_at, OperatorPasskey.id)
        )
    )


def count(db: Session, account_id: uuid.UUID) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(OperatorPasskey)
            .where(OperatorPasskey.account_id == account_id)
        )
        or 0
    )


def _descriptors(
    db: Session, account_id: uuid.UUID
) -> list[PublicKeyCredentialDescriptor]:
    return [
        PublicKeyCredentialDescriptor(id=row.credential_id)
        for row in of_account(db, account_id)
    ]


def registration_options(
    db: Session,
    account_id: uuid.UUID,
    account_name: str,
    session_id: uuid.UUID,
    now: datetime,
) -> str:
    """What `navigator.credentials.create` takes, as JSON. A device that holds
    a passkey of this account already is left out: two passkeys are two
    devices, or losing one would lose both. Commits."""
    options = generate_registration_options(
        rp_id=settings.relying_party_id,
        rp_name=RP_NAME,
        user_id=account_id.bytes,
        user_name=account_name,
        challenge=issue(db, for_session(session_id), REGISTER, now),
        timeout=TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=_descriptors(db, account_id),
    )
    return options_to_json(options)


def register(
    db: Session,
    account_id: uuid.UUID,
    session_id: uuid.UUID,
    answer: str,
    name: str,
    now: datetime,
) -> OperatorPasskey:
    """Check the answer of a registration and add the passkey. Raises
    `Refused`. The challenge the answer names is gone either way; the new row
    waits for the caller's commit, beside its audit."""
    challenge = take(
        db, for_session(session_id), REGISTER, now, signed_challenge(answer)
    )
    name = " ".join(name.split())
    if not 1 <= len(name) <= NAME_MAX or not name.isprintable():
        raise Refused("name")
    # The account's row is held, so two registrations at once count each other.
    # The break glass holds it too: a registration that waited for it here
    # finds its session gone and must not put a passkey back.
    db.get(OperatorAccount, account_id, with_for_update=True)
    alive = db.scalar(
        select(OperatorSession.id).where(OperatorSession.id == session_id)
    )
    if (
        challenge is None
        or alive is None
        or len(answer) > ANSWER_MAX
        or count(db, account_id) >= PASSKEYS_MAX
    ):
        db.rollback()
        raise Refused()
    try:
        credential = parse_registration_credential_json(answer)
        client = parse_client_data_json(credential.response.client_data_json)
        verified = verify_registration_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=settings.relying_party_id,
            expected_origin=settings.operator_origin,
            require_user_verification=True,
        )
    except Exception as problem:  # any error here is a refusal (module docstring)
        db.rollback()
        raise Refused() from problem
    # A page of this site inside another site's frame: the library reads the
    # flag and leaves the decision to us.
    if client.cross_origin:
        db.rollback()
        raise Refused()
    row = OperatorPasskey(
        account_id=account_id,
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        name=name,
        created_at=now,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as taken:
        # The credential is registered already, here or on another account.
        db.rollback()
        raise Refused() from taken
    return row


def assertion_options(
    db: Session,
    bound_to: str,
    purpose: str,
    now: datetime,
    account_id: uuid.UUID | None = None,
    client_address: str | None = None,
) -> str:
    """What `navigator.credentials.get` takes, as JSON. Without an account (the
    sign-in page, where nobody is known yet) it names no credential: the
    device offers the passkeys it holds for this site. Commits; raises
    `TooMany` as `issue` does."""
    options = generate_authentication_options(
        rp_id=settings.relying_party_id,
        challenge=issue(db, bound_to, purpose, now, client_address),
        timeout=TIMEOUT_MS,
        allow_credentials=_descriptors(db, account_id) if account_id else None,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return options_to_json(options)


def verify(
    db: Session,
    account_id: uuid.UUID,
    challenge: bytes | None,
    answer: str,
    now: datetime,
) -> OperatorPasskey:
    """Check an assertion against the challenge taken for it: a passkey of
    this account, this site's origin, the operator verified on the device, a
    counter that grew. Raises `Refused`; the caller commits."""
    if challenge is None or len(answer) > ANSWER_MAX:
        raise Refused()
    try:
        credential = parse_authentication_credential_json(answer)
        client = parse_client_data_json(credential.response.client_data_json)
    except Exception as problem:  # any error here is a refusal (module docstring)
        raise Refused() from problem
    row = db.scalars(
        select(OperatorPasskey)
        .where(
            OperatorPasskey.credential_id == credential.raw_id,
            OperatorPasskey.account_id == account_id,
        )
        .with_for_update()
    ).first()
    if (
        row is None
        or client.cross_origin
        or credential.response.user_handle not in (None, account_id.bytes)
    ):
        raise Refused()
    try:
        # The counter is compared below, not by the library, so that a copy
        # gets its own word in the audit.
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=challenge,
            expected_rp_id=settings.relying_party_id,
            expected_origin=settings.operator_origin,
            credential_public_key=row.public_key,
            credential_current_sign_count=0,
            require_user_verification=True,
        )
    except Exception as problem:  # any error here is a refusal (module docstring)
        raise Refused() from problem
    # Passkeys synced by a platform never count and send 0 every time.
    if (verified.new_sign_count or row.sign_count) and (
        verified.new_sign_count <= row.sign_count
    ):
        raise Refused("counter")
    row.sign_count = verified.new_sign_count
    row.last_used_at = now
    return row

"""A software authenticator for the passkey tests (docs/23 §5): what a device
does in a WebAuthn ceremony, with every part open to tampering.

`create` answers the options of a registration, `get` those of a sign-in or a
step-up, both as the JSON a browser's `credential.toJSON()` gives. The keyword
arguments bend one thing each: another origin, another challenge, another
relying party, no user verification, a counter of the test's choosing.
"""

import hashlib
import json
import secrets
from typing import Any

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

# Flags of the authenticator data: user present, user verified, attested
# credential data included.
UP, UV, AT = 0x01, 0x04, 0x40


class Device:
    def __init__(self, counts: bool = True) -> None:
        """`counts=False` is a passkey a platform syncs: its counter stays 0."""
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.sign_count = 0
        self.counts = counts
        self.user_handle: bytes | None = None

    def _client_data(
        self, kind: str, challenge: str, origin: str, cross_origin: bool
    ) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge,
                "origin": origin,
                "crossOrigin": cross_origin,
            }
        ).encode()

    def _authenticator_data(
        self, rp_id: str, flags: int, sign_count: int | None, attested: bytes = b""
    ) -> bytes:
        if sign_count is not None:
            self.sign_count = sign_count
        elif self.counts:
            self.sign_count += 1
        return (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([flags])
            + self.sign_count.to_bytes(4, "big")
            + attested
        )

    def create(
        self,
        options: str,
        origin: str,
        *,
        verified: bool = True,
        cross_origin: bool = False,
        challenge: str | None = None,
        rp_id: str | None = None,
    ) -> str:
        asked: dict[str, Any] = json.loads(options)
        numbers = self.key.public_key().public_numbers()
        # COSE: an EC2 key on P-256 for ES256.
        public_key = cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )
        attested = (
            bytes(16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + public_key
        )
        data = self._authenticator_data(
            rp_id or asked["rp"]["id"],
            UP | AT | (UV if verified else 0),
            None,
            attested,
        )
        self.user_handle = base64url_to_bytes(asked["user"]["id"])
        client = self._client_data(
            "webauthn.create", challenge or asked["challenge"], origin, cross_origin
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": data})
        return json.dumps(
            {
                "id": bytes_to_base64url(self.credential_id),
                "rawId": bytes_to_base64url(self.credential_id),
                "type": "public-key",
                "response": {
                    "clientDataJSON": bytes_to_base64url(client),
                    "attestationObject": bytes_to_base64url(attestation),
                },
                "clientExtensionResults": {},
            }
        )

    def get(
        self,
        options: str,
        origin: str,
        *,
        verified: bool = True,
        cross_origin: bool = False,
        challenge: str | None = None,
        rp_id: str | None = None,
        sign_count: int | None = None,
    ) -> str:
        asked: dict[str, Any] = json.loads(options)
        data = self._authenticator_data(
            rp_id or asked["rpId"], UP | (UV if verified else 0), sign_count
        )
        client = self._client_data(
            "webauthn.get", challenge or asked["challenge"], origin, cross_origin
        )
        signature = self.key.sign(
            data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256())
        )
        response = {
            "clientDataJSON": bytes_to_base64url(client),
            "authenticatorData": bytes_to_base64url(data),
            "signature": bytes_to_base64url(signature),
        }
        if self.user_handle is not None:
            response["userHandle"] = bytes_to_base64url(self.user_handle)
        return json.dumps(
            {
                "id": bytes_to_base64url(self.credential_id),
                "rawId": bytes_to_base64url(self.credential_id),
                "type": "public-key",
                "response": response,
                "clientExtensionResults": {},
            }
        )

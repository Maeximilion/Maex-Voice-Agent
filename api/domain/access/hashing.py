"""The hash of the restaurant's password, token hashes, and the rules for a new
password and PIN (the PINs are stored readable, migration 031).

scrypt from the standard library with the parameters of docs/23 §5 (n=2^15,
r=8, p=1, 16-byte salt): about 32 MiB and a few dozen milliseconds per check,
which a sign-in can afford and a guesser cannot. Tokens (session, setup link)
are random, so a plain SHA-256 is enough for them.
"""

import base64
import hashlib
import hmac
import re
import secrets
import threading

N, R, P = 2**15, 8, 1
SALT_BYTES = 16
KEY_BYTES = 32
# scrypt needs 128 * n * r bytes (32 MiB here), the default limit is exactly that.
MAX_MEM = 64 * 1024 * 1024
# At most two hashes at once: a burst of sign-ins on the server's thread pool
# (40 threads) would otherwise take over a gigabyte of the stack's memory.
_RUNNING = threading.BoundedSemaphore(2)

PASSWORD_MIN = 12
PASSWORD_MAX = 128
PIN = re.compile(r"[0-9]{4,6}")
# The passwords people pick first; enough to refuse the obvious ones, no file.
COMMON = frozenset(
    {
        "123456789012",
        "1234567890123",
        "passwort1234",
        "password1234",
        "passwort12345",
        "password12345",
        "qwertzuiop12",
        "qwertyuiop12",
        "restaurant12",
        "restaurant123",
        "restaurant1234",
        "abcdefghijkl",
        "aaaaaaaaaaaa",
        "111111111111",
        "000000000000",
        "willkommen12",
        "willkommen123",
        "hallo1234567",
    }
)


def hash_secret(secret: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    key = _scrypt(secret, salt, N, R, P)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(key)}"


def verify_secret(secret: str, stored: str | None) -> bool:
    """True when `secret` matches `stored`. A missing or broken hash still costs
    one scrypt run, so a sign-in before the setup takes as long as a wrong one."""
    try:
        name, n, r, p, salt, key = (stored or "").split("$")
        if name != "scrypt":
            raise ValueError(name)
        expected = base64.b64decode(key)
        actual = _scrypt(secret, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        _scrypt(secret, b"\0" * SALT_BYTES, N, R, P)
        return False
    return hmac.compare_digest(actual, expected)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_problem(password: str) -> str | None:
    """Why a new password is refused, in the team's words, or None."""
    if len(password) < PASSWORD_MIN:
        return f"Das Passwort braucht mindestens {PASSWORD_MIN} Zeichen."
    if len(password) > PASSWORD_MAX:
        return f"Das Passwort darf höchstens {PASSWORD_MAX} Zeichen haben."
    if password.lower() in COMMON or len(set(password)) < 4:
        return "Dieses Passwort ist zu leicht zu erraten. Bitte ein anderes wählen."
    return None


def pin_problem(pin: str) -> str | None:
    if not PIN.fullmatch(pin):
        return "Die PIN hat 4 bis 6 Ziffern."
    return None


def _scrypt(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    with _RUNNING:
        return hashlib.scrypt(
            secret.encode(), salt=salt, n=n, r=r, p=p, maxmem=MAX_MEM, dklen=KEY_BYTES
        )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()

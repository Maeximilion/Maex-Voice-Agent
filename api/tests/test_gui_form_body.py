"""`gui/router.py` `_form` reads at most FORM_MAX bytes of a form (#819), by
its announced length and while a body without one streams in."""

import asyncio

import pytest
from fastapi import HTTPException, Request

from api.gui.router import FORM_MAX, _form


def _request(chunks: list[bytes], length: str | None) -> Request:
    headers = [] if length is None else [(b"content-length", length.encode())]
    pending = list(chunks)

    async def receive() -> dict[str, object]:
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    return Request({"type": "http", "method": "POST", "headers": headers}, receive)


def test_a_form_within_the_limit_is_read_in_its_chunks():
    request = _request([b"a=1&b=", b"x" * 1000, b"&b=2"], None)
    assert asyncio.run(_form(request)) == {"a": "1", "b": "2"}


@pytest.mark.parametrize(
    ("chunks", "length"),
    [
        # No length announced: refused while it streams, not after.
        ([b"x" * 40_000, b"x" * 40_000, b"never read"], None),
        # A length beyond the limit is refused before a byte is read.
        ([b"a=1"], str(FORM_MAX + 1)),
        ([b"a=1"], "9" * 5000),
    ],
)
def test_a_body_beyond_the_limit_is_a_413(chunks, length):
    request = _request(chunks, length)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(_form(request))
    assert refused.value.status_code == 413

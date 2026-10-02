"""Properties of HMAC webhook signing."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from imda.events.signing import DEFAULT_TOLERANCE_SECONDS, sign, verify

secrets = st.text(min_size=1, max_size=64)
bodies = st.binary(max_size=512)
stamps = st.integers(min_value=0, max_value=4_000_000_000)


@given(secrets, bodies, stamps)
def test_a_fresh_signature_verifies(secret: str, body: bytes, ts: int) -> None:
    assert verify(secret, body, sign(secret, body, ts), ts, now=ts)


@given(secrets, bodies)
def test_a_signature_without_timestamp_verifies(secret: str, body: bytes) -> None:
    assert verify(secret, body, sign(secret, body))


@given(secrets, st.binary(min_size=1, max_size=512), stamps, st.data())
def test_any_single_byte_change_in_the_body_is_rejected(
    secret: str, body: bytes, ts: int, data: st.DataObject
) -> None:
    index = data.draw(st.integers(0, len(body) - 1))
    flip = data.draw(st.integers(1, 255))
    tampered = body[:index] + bytes([body[index] ^ flip]) + body[index + 1 :]

    assert not verify(secret, tampered, sign(secret, body, ts), ts, now=ts)


@given(secrets, bodies, stamps, st.data())
def test_any_single_character_change_in_the_signature_is_rejected(
    secret: str, body: bytes, ts: int, data: st.DataObject
) -> None:
    signature = sign(secret, body, ts)
    index = data.draw(st.integers(0, len(signature) - 1))
    replacement = data.draw(st.characters().filter(lambda c: c != signature[index]))
    tampered = signature[:index] + replacement + signature[index + 1 :]

    assert not verify(secret, body, tampered, ts, now=ts)


# Includes lone surrogates: `verify` promises to return False, never raise.
@given(secrets, bodies, stamps, st.text(alphabet=st.characters(), max_size=100))
def test_arbitrary_signature_text_never_raises_and_never_verifies(
    secret: str, body: bytes, ts: int, junk: str
) -> None:
    assert verify(secret, body, junk, ts, now=ts) == (junk == sign(secret, body, ts))


@given(secrets, secrets, bodies, stamps)
def test_a_different_secret_is_rejected(secret: str, other: str, body: bytes, ts: int) -> None:
    signature = sign(secret, body, ts)

    assert verify(other, body, signature, ts, now=ts) == (secret == other)


@given(secrets, bodies, stamps, st.integers(min_value=1, max_value=10_000))
def test_a_signed_timestamp_cannot_be_swapped(
    secret: str, body: bytes, ts: int, shift: int
) -> None:
    signature = sign(secret, body, ts)

    assert not verify(secret, body, signature, ts + shift, now=ts + shift)


@given(secrets, bodies, stamps, st.integers(min_value=-1_000_000, max_value=1_000_000))
def test_the_timestamp_window_is_symmetric_and_enforced(
    secret: str, body: bytes, ts: int, skew: int
) -> None:
    signature = sign(secret, body, ts)

    accepted = verify(secret, body, signature, ts, now=ts + skew)

    assert accepted == (abs(skew) <= DEFAULT_TOLERANCE_SECONDS)


@given(bodies, stamps)
def test_an_empty_secret_never_signs_or_verifies(body: bytes, ts: int) -> None:
    with pytest.raises(ValueError, match="secret"):
        sign("", body, ts)
    assert not verify("", body, "0" * 64, ts, now=ts)

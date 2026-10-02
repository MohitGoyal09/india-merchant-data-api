"""HMAC signing: known vector, verify, replay window, constant-time compare."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from imda.events import signing

SECRET = "s3cret"
BODY = b'{"event":"fx.rates.published","id":"evt_1"}'
TS = 1_790_000_000


def test_sign_matches_known_vector_with_timestamp() -> None:
    expected = hmac.new(SECRET.encode(), f"{TS}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert signing.sign(SECRET, BODY, TS) == expected


def test_sign_without_timestamp_is_plain_body_hmac() -> None:
    expected = hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert signing.sign(SECRET, BODY) == expected


def test_verify_true_for_valid_signature() -> None:
    sig = signing.sign(SECRET, BODY, TS)
    assert signing.verify(SECRET, BODY, sig, timestamp=TS, now=TS + 10)


def test_verify_true_without_timestamp() -> None:
    assert signing.verify(SECRET, BODY, signing.sign(SECRET, BODY))


@pytest.mark.parametrize(
    ("secret", "body", "sig_ts"),
    [("other", BODY, TS), (SECRET, BODY + b" ", TS), (SECRET, BODY, TS + 1)],
)
def test_verify_false_when_secret_body_or_timestamp_differ(
    secret: str, body: bytes, sig_ts: int
) -> None:
    sig = signing.sign(SECRET, BODY, TS)
    assert not signing.verify(secret, body, sig, timestamp=sig_ts, now=sig_ts)


def test_verify_false_for_malformed_signature() -> None:
    assert not signing.verify(SECRET, BODY, "", timestamp=TS, now=TS)
    assert not signing.verify(SECRET, BODY, "not-hex-é", timestamp=TS, now=TS)


def test_timestamp_tolerance_boundaries() -> None:
    sig = signing.sign(SECRET, BODY, TS)
    assert signing.verify(SECRET, BODY, sig, timestamp=TS, now=TS + 300)
    assert not signing.verify(SECRET, BODY, sig, timestamp=TS, now=TS + 301)
    assert signing.verify(SECRET, BODY, sig, timestamp=TS, now=TS - 300)
    assert not signing.verify(SECRET, BODY, sig, timestamp=TS, now=TS - 301)


def test_replay_of_old_delivery_rejected_even_with_valid_signature() -> None:
    sig = signing.sign(SECRET, BODY, TS)
    assert not signing.verify(SECRET, BODY, sig, timestamp=TS, now=TS + 3600)


def test_custom_tolerance() -> None:
    sig = signing.sign(SECRET, BODY, TS)
    assert not signing.verify(SECRET, BODY, sig, timestamp=TS, now=TS + 20, tolerance=10)


def test_verify_defaults_to_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(signing.time, "time", lambda: float(TS))
    assert signing.verify(SECRET, BODY, signing.sign(SECRET, BODY, TS), timestamp=TS)


def test_verify_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(signing.hmac, "compare_digest", spy)
    assert signing.verify(SECRET, BODY, signing.sign(SECRET, BODY, TS), timestamp=TS, now=TS)
    assert len(calls) == 1


def test_sign_refuses_an_empty_secret() -> None:
    with pytest.raises(ValueError, match="secret"):
        signing.sign("", BODY, TS)


def test_verify_is_false_for_an_empty_secret() -> None:
    forged = hmac.new(b"", f"{TS}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert signing.verify("", BODY, forged, timestamp=TS, now=TS) is False

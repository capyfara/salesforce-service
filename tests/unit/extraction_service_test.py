"""Unit tests for auth/hmac_auth.py, calling the dependencies directly (no HTTP)."""
import hashlib
import hmac
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auth.hmac_auth import require_key_id, verify_hmac

CORE = "core-key-" + "x" * 32
ENG = "engineer-key-" + "y" * 32


def make_request(body: bytes = b"", enabled=True, max_age=300):
    settings = SimpleNamespace(
        HMAC_ENABLED=enabled, HMAC_SECRET_KEY_CORE=CORE,
        HMAC_SECRET_KEY_ENGINEER=ENG, HMAC_SIGNATURE_MAX_AGE=max_age,
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings)),
        state=SimpleNamespace(raw_body=body),
    )


def sig(ts, body="", key=CORE):
    return hmac.new(key.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()


def call(req, signature, ts, key_id="core"):
    return verify_hmac(req, x_signature=signature, x_timestamp=str(ts), x_key_id=key_id)


def test_valid_core_signature_records_key_id():
    req, ts = make_request(b"{}"), time.time()
    call(req, sig(ts, "{}"), ts)
    assert req.state.hmac_key_id == "core"


def test_valid_engineer_signature_records_key_id():
    req, ts = make_request(), time.time()
    call(req, sig(ts, key=ENG), ts, key_id="engineer")
    assert req.state.hmac_key_id == "engineer"


def test_disabled_hmac_short_circuits_even_with_garbage():
    req = make_request(enabled=False)
    assert call(req, "garbage", 0) is None
    assert not hasattr(req.state, "hmac_key_id")


def test_unknown_key_id_rejected():
    with pytest.raises(HTTPException) as e:
        call(make_request(), "x", time.time(), key_id="intruder")
    assert (e.value.status_code, e.value.detail) == (401, "Unknown key id")


def test_expired_signature_rejected():
    ts = time.time() - 301
    with pytest.raises(HTTPException) as e:
        call(make_request(), sig(ts), ts)
    assert e.value.detail == "Signature has expired"


def test_signature_just_inside_window_accepted():
    ts = time.time() - 290
    call(make_request(), sig(ts), ts)


def test_future_timestamp_rejected():
    ts = time.time() + 3600
    with pytest.raises(HTTPException) as e:
        call(make_request(), sig(ts), ts)
    assert e.value.status_code == 401


def test_max_age_is_configurable():
    ts = time.time() - 30
    with pytest.raises(HTTPException):
        call(make_request(max_age=10), sig(ts), ts)


def test_wrong_secret_rejected():
    ts = time.time()
    with pytest.raises(HTTPException) as e:
        call(make_request(), sig(ts, key="nope"), ts)
    assert e.value.detail == "Invalid signature"


def test_cross_key_use_rejected():
    ts = time.time()
    with pytest.raises(HTTPException):
        call(make_request(), sig(ts, key=CORE), ts, key_id="engineer")


def test_body_tampering_rejected():
    ts = time.time()
    with pytest.raises(HTTPException):
        call(make_request(b"tampered"), sig(ts, "original"), ts)


def test_timestamp_is_part_of_signed_string():
    ts = time.time()
    with pytest.raises(HTTPException):
        call(make_request(), sig(ts - 5), ts)


@pytest.mark.xfail(reason="float(x_timestamp) is unguarded: non-numeric timestamp raises "
                          "ValueError (-> HTTP 500) instead of 401.", strict=False)
def test_non_numeric_timestamp_is_401():
    with pytest.raises(HTTPException) as e:
        verify_hmac(make_request(), x_signature="x", x_timestamp="abc", x_key_id="core")
    assert e.value.status_code == 401


# --- require_key_id -------------------------------------------------------
def test_require_key_id_allows_matching_key():
    req = make_request()
    req.state.hmac_key_id = "engineer"
    assert require_key_id("engineer")(req, _verified=None) is None


def test_require_key_id_forbids_other_key():
    req = make_request()
    req.state.hmac_key_id = "core"
    with pytest.raises(HTTPException) as e:
        require_key_id("engineer")(req, _verified=None)
    assert e.value.status_code == 403
    assert "engineer" in e.value.detail


def test_require_key_id_forbids_when_no_key_recorded():
    with pytest.raises(HTTPException) as e:
        require_key_id("engineer")(make_request(), _verified=None)
    assert e.value.status_code == 403


def test_require_key_id_skipped_when_hmac_disabled():
    assert require_key_id("engineer")(make_request(enabled=False), _verified=None) is None
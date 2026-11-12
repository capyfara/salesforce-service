import hashlib
import hmac
import math
import re
import time

from fastapi import Depends, Header, HTTPException, Request

NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# None = unrestricted. Method is part of the signed string, so it can't be swapped.
KEY_METHODS: dict[str, frozenset[str] | None] = {
    "core": None,
    "engineer": READ_ONLY_METHODS,
}


def build_signing_string(timestamp: str, nonce: str, key_id: str,
                         method: str, path_query: str, body: bytes) -> str:
    return "\n".join([
        "v1", timestamp, nonce, key_id, method.upper(), path_query,
        hashlib.sha256(body).hexdigest(),
    ])


def sign_request(secret: str, key_id: str, method: str, path_query: str,
                 body: bytes = b"", timestamp: str | None = None,
                 nonce: str | None = None) -> dict:
    """Used by tests and CoreServiceClient; mirrors the Postman script below."""
    import secrets
    ts = timestamp or str(time.time())
    nonce = nonce or secrets.token_hex(16)
    s = build_signing_string(ts, nonce, key_id, method, path_query, body)
    sig = hmac.new(secret.encode(), s.encode(), hashlib.sha256).hexdigest()
    return {"X-Signature": sig, "X-Timestamp": ts, "X-Nonce": nonce, "X-Key-Id": key_id}


def verify_hmac(
    request: Request,
    x_signature: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
    x_nonce: str | None = Header(default=None),
    x_key_id: str = Header(default="core"),
):
    settings = request.app.state.settings
    if not settings.HMAC_ENABLED:
        return

    if not x_signature or not x_timestamp or not x_nonce:
        raise HTTPException(401, "Missing signature headers")

    if x_key_id not in KEY_METHODS:
        raise HTTPException(401, "Unknown key id")
    key = {
        "core": settings.HMAC_SECRET_KEY_CORE,
        "engineer": settings.HMAC_SECRET_KEY_ENGINEER,
    }[x_key_id]

    if not NONCE_RE.match(x_nonce):
        raise HTTPException(401, "Invalid nonce")

    try:
        ts = float(x_timestamp)
    except ValueError:
        raise HTTPException(401, "Invalid timestamp")
    if not math.isfinite(ts):  # float("nan") would otherwise pass the age check
        raise HTTPException(401, "Invalid timestamp")
    if abs(time.time() - ts) > settings.HMAC_SIGNATURE_MAX_AGE:
        raise HTTPException(401, "Signature has expired")

    url = request.url
    path_query = url.path + (f"?{url.query}" if url.query else "")
    signing_string = build_signing_string(
        x_timestamp, x_nonce, x_key_id, request.method, path_query,
        request.state.raw_body,
    )
    expected = hmac.new(key.encode(), signing_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(401, "Invalid signature")

    # Authenticated from here on.
    allowed = KEY_METHODS[x_key_id]
    if allowed is not None and request.method.upper() not in allowed:
        raise HTTPException(403, f"The '{x_key_id}' key is read-only")

    # Consume the nonce only after the signature checks out, so unauthenticated
    # callers can't fill the table. Retention must cover the full validity window
    # (timestamp may be up to MAX_AGE in the future, then valid MAX_AGE more).
    try:
        fresh = request.app.state.nonce_store.consume(
            x_key_id, x_nonce, ttl_seconds=2 * settings.HMAC_SIGNATURE_MAX_AGE
        )
    except Exception:
        raise HTTPException(503, "Auth backend unavailable")  # fail closed
    if not fresh:
        raise HTTPException(401, "Nonce already used")

    request.state.hmac_key_id = x_key_id


def require_key_id(required: str):
    def _check(request: Request, _verified=Depends(verify_hmac)):
        if not request.app.state.settings.HMAC_ENABLED:
            return
        if getattr(request.state, "hmac_key_id", None) != required:
            raise HTTPException(403, f"This endpoint requires the '{required}' key")
    return _check
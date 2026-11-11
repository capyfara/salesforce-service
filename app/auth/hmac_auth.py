import hmac, hashlib, time
from fastapi import Request, HTTPException, Header, Depends

def verify_hmac(
    request: Request,
    x_signature: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
    x_key_id: str = Header(default="core"),
):
    settings = request.app.state.settings
    if not settings.HMAC_ENABLED:
        return

    if not x_signature or not x_timestamp:
        raise HTTPException(401, "Missing signature headers")

    keys = {
        "core": settings.HMAC_SECRET_KEY_CORE,
        "engineer": settings.HMAC_SECRET_KEY_ENGINEER,
    }
    key = keys.get(x_key_id)
    if not key:
        raise HTTPException(401, "Unknown key id")

    try:
        ts = float(x_timestamp)
    except ValueError:
        raise HTTPException(401, "Invalid timestamp")

    if abs(time.time() - ts) > settings.HMAC_SIGNATURE_MAX_AGE:
        raise HTTPException(401, "Signature has expired")

    body = request.state.raw_body
    signing_string = f"{x_timestamp}.{body.decode()}"
    expected = hmac.new(key.encode(), signing_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(401, "Invalid signature")

    request.state.hmac_key_id = x_key_id

def require_key_id(required: str):
    def _check(request: Request, _verified=Depends(verify_hmac)):
        settings = request.app.state.settings
        if not settings.HMAC_ENABLED:
            return
        if getattr(request.state, "hmac_key_id", None) != required:
            raise HTTPException(403, f"This endpoint requires the '{required}' key")
    return _check
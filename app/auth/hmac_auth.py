import hmac, hashlib, time
from fastapi import Request, HTTPException, Header, Depends

def verify_hmac(
    request: Request,
    x_signature: str = Header(...),
    x_timestamp: str = Header(...),
    x_key_id: str = Header(default="core"),
):
    settings = request.app.state.settings
    if not settings.HMAC_ENABLED:
        return

    keys = {
        "core": settings.HMAC_SECRET_KEY_CORE,
        "engineer": settings.HMAC_SECRET_KEY_ENGINEER,
    }

    key = keys.get(x_key_id)
    if not key:
        raise HTTPException(401, "Unknown key id")

    if abs(time.time() - float(x_timestamp)) > settings.HMAC_SIGNATURE_MAX_AGE:
        raise HTTPException(401, "Signature has expired")

    body = request.state.raw_body
    signing_string = f"{x_timestamp}.{body.decode()}"
    expected = hmac.new(key.encode(), signing_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(401, "Invalid signature")

    # record which key_id actually verified, so endpoint-level dependencies
    request.state.hmac_key_id = x_key_id

def require_key_id(required: str):
    def _check(request: Request, _verified=Depends(verify_hmac)):
        settings = request.app.state.settings
        if not settings.HMAC_ENABLED:
            return
        if getattr(request.state, "hmac_key_id", None) != required:
            raise HTTPException(403, f"This endpoint requires the '{required}' key")
    return _check
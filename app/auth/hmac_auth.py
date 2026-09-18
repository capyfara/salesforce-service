import hmac, hashlib, time
from fastapi import Request, HTTPException, Header

def verify_hmac(
    request: Request,
    x_signature: str = Header(...),
    x_timestamp: str = Header(...),
    x_key_id: str = Header(default="core"),
):
    
    settings = request.app.state.settings
    if not settings.HMAC_ENABLED:
        return
    
    key = {
        "core": settings.HMAC_SECRET_KEY_CORE,
        "engineer": settings.HMAC_SECRET_KEY_ENGINEER.get(x_key_id)
    }
    
    if not key:
        raise HTTPException(401, "Unknown key id")
    
    if abs(time.time() - float(x_timestamp)) > settings.HMAC_SIGNATURE_MAX_AGE:
        raise HTTPException(401, "Signature has expired")
    
    body = request.state.raw_body
    signing_string = f"{x_timestamp}.{body.decode()}"
    expected = hmac.new(key.encode(), signing_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(401, "Invalid signature")
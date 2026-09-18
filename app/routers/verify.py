'''	Verify HMAC key permissions'''

import logging
from fastapi import APIRouter, Request, Depends, Header
from auth.hmac_auth import verify_hmac

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/api/key/verify", dependencies=[Depends(verify_hmac)])
def verify_key(request: Request, x_key_id: str = Header(default="core")):
    settings = request.app.state.settings
    return {
        "valid": True,
        "key_id": x_key_id,
        "hmac_enabled": settings.HMAC_ENABLED,
        "permissions": {
            "core": ["scan:start", "scan:list", "scan:status", "scan:cancel", "scan:resume", "scan:remove", "scan:statistics"],
            "engineer": ["maintenance:cleanup", "scan:start", "scan:list", "scan:status", "scan:cancel", "scan:resume", "scan:remove", "scan:statistics"],
        }.get(x_key_id, []),
    }
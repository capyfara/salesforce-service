import logging
from fastapi import APIRouter, Depends, Request
from dependencies import get_token_manager

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/api/health")
async def health_check(request: Request,token_manager = Depends(get_token_manager)):
    # Check if salesforce is connected
    try: 
        token= token_manager.get_token()
        salesforce_connected = bool(token)
        minio_connected = bool(request.app.state.minio_client.check_bucket())
    except Exception as e:
        logger.warning(f"Health check:  Salesforce token fetch failed: {e}")
        salesforce_connected = False
    
    status = "healthy" if salesforce_connected and minio_connected else "degraded"

    return {
        "status": status,
        "service": "salesforce_service",
        "version" : "1.0.0",
        "salesforce_connected" : salesforce_connected,
        "minio_connected": minio_connected,
    }

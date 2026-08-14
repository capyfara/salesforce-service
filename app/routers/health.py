import logging
from fastapi import APIRouter, Depends
from dependencies import get_token_manager

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/api/health")
async def health_check(token_manager = Depends(get_token_manager)):
    # Check if salesforce is connected
    try: 
        token, instance_url = token_manager.get_token()
        salesforce_connected = bool(token)
    except Exception as e:
        logger.warning(f"Health check:  Salesforce token fetch failed: {e}")
        salesforce_connected = False
    
    status = "healthy" if salesforce_connected else "degraded"

    return {
        "status": status,
        "service": "salesforce_service",
        "version" : "1.0.0",
        "salesforce_connected" : salesforce_connected,
        "minio_connected": None,
    }

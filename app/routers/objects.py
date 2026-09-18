'''List supported Salesforce objects'''

from fastapi import APIRouter, Depends
from auth.hmac_auth import verify_hmac
from models.objects import SUPPORTED_OBJECTS

router = APIRouter(dependencies=[Depends(verify_hmac)])

@router.get("/api/objects")
def list_supported_objects():
    return {"supported_objects":  SUPPORTED_OBJECTS}
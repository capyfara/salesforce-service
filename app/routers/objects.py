'''List supported Salesforce objects'''

from fastapi import APIRouter
from models.objects import SUPPORTED_OBJECTS

router = APIRouter()

@router.get("/api/objects")
def list_supported_objects():
    return {"supported_objects":  SUPPORTED_OBJECTS}
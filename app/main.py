# App factory, Startup validation

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from config import SalesforceServiceSettings, validate_settings
from auth.salesforce_auth import SalesforceTokenManager
from storage.scan_store import InMemoryScanStore
from clients.bulk_api_client import SalesforceBulkAPIClient

from routers import health, objects, scan, status

logger = logging.getLogger(__name__)

# Lifespan event handler to run validation before app startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    errors = validate_settings()
    if errors:
        logger.error("=" * 80)
        logger.error("[ERROR] CONFIGURATION VALIDATION FAILED")
        logger.error("=" * 80)
        for field, details in errors.items():
            logger.error(f"\n[{field}]")
            logger.error(f"  Error: {details['error']}")
            logger.error(f"  Fix:   {details['fix']}")
        logger.error("=" * 80)
        logger.error("Fix the errors above and restart the service.")
        logger.error("=" * 80)
        raise RuntimeError("Configuration validation failed")
    
    # SET THESE UP USING DEPENDS
    
    settings = SalesforceServiceSettings() # pyright: ignore[reportCallIssue]
    private_key = Path(settings.SF_PRIVATE_KEY_PEM_PATH).read_text(encoding="utf-8") # Note change later for prod

    app.state.settings = settings
    token_manager = app.state.token_manager = SalesforceTokenManager (
        settings.SF_CONSUMER_KEY, 
        private_key,
        settings.SF_USERNAME,
        settings.SF_LOGIN_URL
    )
    app.state.bulk_client = SalesforceBulkAPIClient(token_manager)
    app.state.scan_store = InMemoryScanStore()
    logger.info("Startup validation passed, Salesforce token manager initialized")


    yield
    # (place any shutdown/cleanup logic here later)

app = FastAPI(lifespan=lifespan)
app.include_router(health.router)
app.include_router(objects.router)
app.include_router(scan.router)
app.include_router(status.router)
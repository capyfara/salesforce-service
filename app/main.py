# App factory, Startup validation

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request

from storage.clickhouse_client import ClickHouseClient
from config import SalesforceServiceSettings, validate_settings
from auth.salesforce_auth import SalesforceTokenManager
from storage.scan_store import PostgresScanStore
from clients.bulk_api_client import SalesforceBulkAPIClient
from storage.minio_client import MinioStorageClient
from clients.core_service_client import CoreServiceClient

from routers import health, objects, scan, verify, statistics, batch, cleanup

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
    
    settings = SalesforceServiceSettings() # pyright: ignore[reportCallIssue]
    app.state.settings = settings

    private_key = Path(settings.SF_PRIVATE_KEY_PEM_PATH).read_text(encoding="utf-8") # Note change later for prod
    token_manager = app.state.token_manager = SalesforceTokenManager (
        settings.SF_CONSUMER_KEY, 
        private_key,
        settings.SF_USERNAME,
        settings.SF_LOGIN_URL,
    )
    app.state.bulk_client = SalesforceBulkAPIClient(token_manager)
    dsn = (
        f"host={settings.DB_HOST} port={settings.DB_PORT} dbname={settings.DB_NAME} "
        f"user={settings.DB_USER} password={settings.DB_PASSWORD} "
        f"options=-csearch_path={settings.DB_SCHEMA}"
    )
    app.state.scan_store = PostgresScanStore(dsn)
    app.state.minio_client = MinioStorageClient(
        endpoint=settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
        bucket=settings.MINIO_BUCKET,
    )
    app.state.core_client = CoreServiceClient(
        settings.CORE_SERVICE_URL, 
        settings.HMAC_SECRET_KEY_CORE
    )

    if settings.CLICKHOUSE_ENABLED:
        s3_base = settings.CLICKHOUSE_S3_ENDPOINT or (
            f"{'https' if settings.MINIO_SECURE else 'http'}://{settings.MINIO_ENDPOINT}"
        )
        ch = ClickHouseClient(
            settings.CLICKHOUSE_HOST, settings.CLICKHOUSE_PORT,
            settings.CLICKHOUSE_USER, settings.CLICKHOUSE_PASSWORD,
            settings.CLICKHOUSE_DATABASE, settings.CLICKHOUSE_SECURE,
            s3_base, settings.MINIO_BUCKET,
            settings.MINIO_ACCESS_KEY, settings.MINIO_SECRET_KEY,
        )
        ch.ensure_schema()
        app.state.clickhouse_client = ch
    else:
        app.state.clickhouse_client = None
   
    logger.info("Startup validation passed, Salesforce token manager initialized")


    yield
    # (place any shutdown/cleanup logic here later)

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def capture_raw_body(request: Request, call_next):
    request.state.raw_body = await request.body()
    response = await call_next(request)
    return response

app.include_router(health.router) # /api/health
app.include_router(verify.router) # /api/key/verify
app.include_router(scan.router) # /api/scan/* endpoints
app.include_router(statistics.router) # /api/statistics
app.include_router(cleanup.router)# /api/maintenance/cleanup
app.include_router(objects.router) # /api/objects
app.include_router(batch.router) # /api/batch/info
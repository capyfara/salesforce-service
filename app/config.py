# Settings class with Pydantic validation

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import ValidationError, Field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

class SalesforceServiceSettings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", 
        env_file_encoding='utf-8', 
        extra="ignore"
    )
    
    # FastAPI
    FASTAPI_ENV:str = Field(default="development")
    FASTAPI_DEBUG:bool
    SECRET_KEY:str
    PORT:int
    HOST:str
    ENVIRONMENT:str

    # Logging
    LOG_LEVEL:str
    LOG_FORMAT:str
    LOKI_ENABLED:bool

    # # Database
    # DB_HOST:str
    # DB_PORT:int
    # DB_NAME:str
    # DB_PASSWORD:str
    # DB_SCHEMA:str

    # Salesforce
    SF_CONSUMER_KEY:str
    SF_PRIVATE_KEY_PEM_PATH:str
    SF_USERNAME:str
    SF_LOGIN_URL:str
    SF_API_VERSION:str
    SF_BULK_PAGE_SIZE:int
    SF_MAX_JOB_TIMEOUT_HOURS:int

    # # Scan behavior
    MAX_CONCURRENT_SCANS:int
    SCAN_TIMEOUT_HOURS:int
    CLEANUP_DAYS:int

    # # HMAC (Hash based message authentication code)
    # HMAC_ENABLED:bool
    # HMAC_SECRET_KEY_CORE:str
    # HMAC_SECRET_KEY_ENGINEER:str
    # HMAC_SIGNATURE_MAX_AGE:int

    # # MinIO
    MINIO_ENABLED:bool
    MINIO_ENDPOINT:str
    MINIO_ACCESS_KEY:str
    MINIO_SECRET_KEY:str
    MINIO_SECURE:bool
    MINIO_BUCKET:str

    # # Kafka
    # KAFKA_BOOTSTRAP_SERVERS:str
    # KAFKA_CONSUMER_GROUP_ID:str
    # KAFKA_AUTO_OFFSET_RESET:str
    # KAFKA_ENABLE_AUTO_COMMIT:bool

    # # Clickhouse
    # CLICKHOUSE_ENABLED:bool
    # CLICKHOUSE_HOST: str
    # CLICKHOUSE_PORT: int
    # CLICKHOUSE_USER: str
    # CLICKHOUSE_PASSWORD: str
    # CLICKHOUSE_DATABASE: str
    
    # # Core Service
    # ALLOWED_ORIGINS: str
    # BD_CORE_URL: str

    # where to handle vault secret paths? what does it mean

# This function returns configuration errors
# Format is a dictionary {field, details{"error", "fix"}}
def validate_settings():
    errors: dict = {}
    try:
        settings = SalesforceServiceSettings() # pyright: ignore[reportCallIssue]
    except ValidationError as e:
        for err in  getattr(e, "errors", lambda: [])():
            field = ".".join(str(loc) for loc in err["loc"])
            errors[field] = {
                "error": err["msg"],
                "fix": f"Set the {field} environment variable (see .env.example)",
            }
        if not errors:
            errors["unknown"] = {"error": str(e), "fix": "Check your .env file"}
        return errors
    # cross-field / environment specific rules here

    if settings.FASTAPI_ENV in ("stage", "prod") and settings.FASTAPI_DEBUG:
        errors["FASTAPI_DEBUG"] = {
            "error": "FASTAPI_DEBUG must be False in stage/prod",
            "fix": "Set FASTAPI_DEBUG=false",
        }
    # if settings.FASTAPI_ENV in ("stage", "prod") and not settings.HMAC_ENABLED:
    #     errors["HMAC_ENABLED"] = {
    #         "error": "HMAC_ENABLED must be true in stage/prod",
    #         "fix": "Set HMAC_ENABLED=true",
    #     }
    # if settings.FASTAPI_ENV == "prod" and "test.salesforce.com" in settings.sf_login_url:
    #     errors["SF_LOGIN_URL"] = {
    #         "error": "Production must not point at Salesforce sandbox",
    #         "fix": "Set SF_LOGIN_URL=https://login.salesforce.com",
    #     }

    return errors
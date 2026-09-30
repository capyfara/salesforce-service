# Settings class with Pydantic validation

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import ValidationError, Field, model_validator
from pathlib import Path
import base64

BASE_DIR = Path(__file__).resolve().parent.parent

class SalesforceServiceSettings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", 
        env_file_encoding='utf-8', 
        extra="ignore"
    )
    
    # FastAPI
    FASTAPI_ENV: str = Field(default="development")
    FASTAPI_DEBUG: bool = False
    SECRET_KEY: str
    PORT: int
    HOST: str
    ENVIRONMENT:str

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "text"
    LOKI_ENABLED: bool = False

    # # Database
    DB_HOST: str
    DB_PORT: int
    DB_NAME: str
    DB_USER :str
    DB_PASSWORD: str
    DB_SCHEMA: str  = "public"

    # Salesforce
    SF_CONSUMER_KEY: str
    SF_PRIVATE_KEY_PEM: str | None = None        # Vault/Nomad: base64-encoded PEM
    SF_PRIVATE_KEY_PEM_PATH: str | None = None   # local dev: path to a PEM file
    SF_USERNAME: str
    SF_LOGIN_URL: str
    SF_API_VERSION: str
    SF_BULK_PAGE_SIZE: int = 50000
    SF_MAX_JOB_TIMEOUT_HOURS: int = 2
    # # Scan behavior
    MAX_CONCURRENT_SCANS: int = 2
    SCAN_TIMEOUT_HOURS: int = 2
    CLEANUP_DAYS: int = 7
    # # HMAC
    HMAC_ENABLED: bool = False
    HMAC_SECRET_KEY_CORE: str
    HMAC_SECRET_KEY_ENGINEER: str
    HMAC_SIGNATURE_MAX_AGE: int = 300

    # # MinIO
    MINIO_ENABLED: bool = False
    MINIO_ENDPOINT: str
    MINIO_ACCESS_KEY: str
    MINIO_SECRET_KEY: str
    MINIO_SECURE: bool = False
    MINIO_BUCKET: str

    # Clickhouse
    CLICKHOUSE_ENABLED: bool = False
    CLICKHOUSE_HOST: str | None = None
    CLICKHOUSE_PORT: int | None = None
    CLICKHOUSE_USER: str | None = None
    CLICKHOUSE_PASSWORD: str | None = None
    CLICKHOUSE_DATABASE: str | None = None
    
    # # Core Service
    ALLOWED_ORIGINS: str
    CORE_SERVICE_URL: str

    @model_validator(mode="after")
    def _check_sf_key_source(self):
        if not self.SF_PRIVATE_KEY_PEM and not self.SF_PRIVATE_KEY_PEM_PATH:
            raise ValueError("Set either SF_PRIVATE_KEY_PEM or SF_PRIVATE_KEY_PEM_PATH")
        return self

    def load_sf_private_key(self) -> str:
        """Returns decoded PEM text regardless of which source was configured."""
        if self.SF_PRIVATE_KEY_PEM:
            return base64.b64decode(self.SF_PRIVATE_KEY_PEM).decode("utf-8")
        return Path(self.SF_PRIVATE_KEY_PEM_PATH).read_text(encoding="utf-8")


def validate_settings():
    errors: dict = {}
    try:
        settings = SalesforceServiceSettings()  # pyright: ignore[reportCallIssue]
    except ValidationError as e:
        for err in getattr(e, "errors", lambda: [])():
            field = ".".join(str(loc) for loc in err["loc"])
            errors[field] = {
                "error": err["msg"],
                "fix": f"Set the {field} environment variable (see .env.example)",
            }
        if not errors:
            errors["unknown"] = {"error": str(e), "fix": "Check your .env file"}
        return errors

    if settings.ENVIRONMENT in ("stage", "prod") and settings.FASTAPI_DEBUG:
        errors["FASTAPI_DEBUG"] = {
            "error": "FASTAPI_DEBUG must be False in stage/prod",
            "fix": "Set FASTAPI_DEBUG=false",
        }
    if settings.ENVIRONMENT in ("stage", "prod") and not settings.HMAC_ENABLED:
        errors["HMAC_ENABLED"] = {
            "error": "HMAC_ENABLED must be true in stage/prod",
            "fix": "Set HMAC_ENABLED=true",
        }
    if settings.ENVIRONMENT == "prod" and "test.salesforce.com" in settings.SF_LOGIN_URL:
        errors["SF_LOGIN_URL"] = {
            "error": "Production must not point at Salesforce sandbox",
            "fix": "Set SF_LOGIN_URL=https://login.salesforce.com",
        }

    return errors
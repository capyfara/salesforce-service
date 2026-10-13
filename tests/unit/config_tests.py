"""Unit tests for config.validate_settings (fail-fast startup validation)."""
import pytest

import config

VALID_ENV = {
    "FASTAPI_ENV": "development", "FASTAPI_DEBUG": "true", "SECRET_KEY": "s" * 32,
    "PORT": "5710", "HOST": "0.0.0.0", "ENVIRONMENT": "dev",
    "LOG_LEVEL": "DEBUG", "LOG_FORMAT": "text", "LOKI_ENABLED": "false",
    "DB_HOST": "db", "DB_PORT": "5432", "DB_NAME": "n", "DB_USER": "u",
    "DB_PASSWORD": "p", "DB_SCHEMA": "public",
    "SF_CONSUMER_KEY": "ck", "SF_PRIVATE_KEY_PEM_PATH": "/tmp/k.pem",
    "SF_USERNAME": "u@x.com", "SF_LOGIN_URL": "https://test.salesforce.com",
    "SF_API_VERSION": "v59.0", "SF_BULK_PAGE_SIZE": "50000", "SF_MAX_JOB_TIMEOUT_HOURS": "2",
    "MAX_CONCURRENT_SCANS": "2", "SCAN_TIMEOUT_HOURS": "2", "CLEANUP_DAYS": "7",
    "HMAC_ENABLED": "false", "HMAC_SECRET_KEY_CORE": "c", "HMAC_SECRET_KEY_ENGINEER": "e",
    "HMAC_SIGNATURE_MAX_AGE": "300",
    "MINIO_ENABLED": "true", "MINIO_ENDPOINT": "minio:9000", "MINIO_ACCESS_KEY": "a",
    "MINIO_SECRET_KEY": "s", "MINIO_SECURE": "false", "MINIO_BUCKET": "b",
    "CLICKHOUSE_ENABLED": "true", "CLICKHOUSE_HOST": "ch", "CLICKHOUSE_PORT": "8123",
    "CLICKHOUSE_USER": "u", "CLICKHOUSE_PASSWORD": "p", "CLICKHOUSE_DATABASE": "salesforce",
    "ALLOWED_ORIGINS": "*", "CORE_SERVICE_URL": "http://localhost:5700",
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    # Isolate from the machine's real environment and any .env file.
    for k in list(VALID_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setitem(config.SalesforceServiceSettings.model_config, "env_file", tmp_path / "none.env")

    def apply(**overrides):
        for k, v in {**VALID_ENV, **overrides}.items():
            monkeypatch.setenv(k, v)
    return apply


def test_valid_config_has_no_errors(env):
    env()
    assert config.validate_settings() == {}


def test_settings_are_parsed_to_declared_types(env):
    env(PORT="1234", HMAC_ENABLED="true", MINIO_SECURE="false")
    s = config.SalesforceServiceSettings()
    assert s.PORT == 1234 and s.HMAC_ENABLED is True and s.MINIO_SECURE is False


def test_missing_variable_reported_with_fix(env, monkeypatch):
    env()
    monkeypatch.delenv("SECRET_KEY")
    errors = config.validate_settings()
    assert "SECRET_KEY" in errors
    assert "SECRET_KEY" in errors["SECRET_KEY"]["fix"]
    assert errors["SECRET_KEY"]["error"]


def test_all_missing_variables_reported_together(env, monkeypatch):
    env()
    for k in ("SECRET_KEY", "DB_HOST", "SF_USERNAME"):
        monkeypatch.delenv(k)
    assert {"SECRET_KEY", "DB_HOST", "SF_USERNAME"} <= set(config.validate_settings())


def test_wrongly_typed_value_reported(env):
    env(PORT="not-a-number")
    assert "PORT" in config.validate_settings()


def test_error_entries_have_error_and_fix_keys(env, monkeypatch):
    env()
    monkeypatch.delenv("PORT")
    for details in config.validate_settings().values():
        assert set(details) == {"error", "fix"}


@pytest.mark.parametrize("env_name", ["stage", "prod"])
def test_debug_forbidden_in_stage_and_prod(env, env_name):
    env(ENVIRONMENT=env_name, FASTAPI_DEBUG="true", HMAC_ENABLED="true")
    errors = config.validate_settings()
    assert "FASTAPI_DEBUG" in errors
    assert "HMAC_ENABLED" not in errors


@pytest.mark.parametrize("env_name", ["stage", "prod"])
def test_hmac_required_in_stage_and_prod(env, env_name):
    env(ENVIRONMENT=env_name, FASTAPI_DEBUG="false", HMAC_ENABLED="false")
    assert "HMAC_ENABLED" in config.validate_settings()


@pytest.mark.parametrize("env_name", ["stage", "prod"])
def test_secure_config_allowed_in_stage_and_prod(env, env_name):
    env(ENVIRONMENT=env_name, FASTAPI_DEBUG="false", HMAC_ENABLED="true",  SF_LOGIN_URL="https://login.salesforce.com")
    assert config.validate_settings() == {}


def test_prod_must_not_use_sandbox_login_url(env):
    env(ENVIRONMENT="prod", FASTAPI_DEBUG="false", HMAC_ENABLED="true",
        SF_LOGIN_URL="https://test.salesforce.com")
    assert "SF_LOGIN_URL" in config.validate_settings()


def test_debug_and_no_hmac_allowed_in_dev(env):
    env(ENVIRONMENT="dev", FASTAPI_DEBUG="true", HMAC_ENABLED="false")
    assert config.validate_settings() == {}

def test_extra_env_vars_are_ignored(env, monkeypatch):
    env()
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")  # present in docker_compose, not in Settings
    assert config.validate_settings() == {}


@pytest.mark.xfail(reason="The stage/prod checks are keyed on FASTAPI_ENV, but the Nomad/compose "
                          "files set ENVIRONMENT=stage|prod and FASTAPI_ENV=production/development, "
                          "so the debug rule never fires on deployed envs.", strict=False)
def test_debug_forbidden_when_environment_is_prod(env):
    env(ENVIRONMENT="prod", FASTAPI_ENV="production", FASTAPI_DEBUG="true")
    assert "FASTAPI_DEBUG" in config.validate_settings()
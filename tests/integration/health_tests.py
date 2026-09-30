"""
Integration tests for GET /api/health

Salesforce token manager and MinIO client are mocked (external boundaries).
The health route has no auth dependency, so no HMAC override is needed.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import health


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def token_manager():
    tm = MagicMock()
    tm.get_token.return_value = ("00Dfake-token", "https://fake.my.salesforce.com")
    return tm


@pytest.fixture
def minio_client():
    mc = MagicMock()
    mc.check_bucket.return_value = True
    return mc


@pytest.fixture
def app(token_manager, minio_client):
    app = FastAPI()
    app.include_router(health.router)
    app.state.token_manager = token_manager
    app.state.minio_client = minio_client
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Healthy path
# ---------------------------------------------------------------------------
def test_health_all_dependencies_up(client):
    resp = client.get("/api/health")
    body = resp.json()

    assert resp.status_code == 200
    assert body["status"] == "healthy"
    assert body["service"] == "salesforce_service"
    assert body["version"] == "1.0.0"
    assert body["salesforce_connected"] is True
    assert body["minio_connected"] is True


def test_health_response_shape(client):
    body = client.get("/api/health").json()
    assert set(body) == {"status", "service", "version", "salesforce_connected", "minio_connected"}


def test_health_requires_no_auth_headers(client):
    """Nomad's health check sends no HMAC headers."""
    assert client.get("/api/health").status_code == 200


def test_health_checks_both_dependencies(client, token_manager, minio_client):
    client.get("/api/health")
    token_manager.get_token.assert_called_once()
    minio_client.check_bucket.assert_called_once()


def test_health_only_allows_get(client):
    assert client.post("/api/health").status_code == 405


# ---------------------------------------------------------------------------
# Degraded paths
# ---------------------------------------------------------------------------
def test_health_salesforce_down(client, token_manager):
    token_manager.get_token.side_effect = Exception("invalid_grant")

    body = client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert body["salesforce_connected"] is False
    assert body["minio_connected"] is True  # MinIO is still checked independently


def test_health_minio_down_exception(client, minio_client):
    minio_client.check_bucket.side_effect = ConnectionError("minio unreachable")

    body = client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert body["salesforce_connected"] is True
    assert body["minio_connected"] is False


def test_health_minio_bucket_missing(client, minio_client):
    """check_bucket returning False (bucket doesn't exist) counts as down."""
    minio_client.check_bucket.return_value = False

    body = client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert body["minio_connected"] is False


def test_health_both_down(client, token_manager, minio_client):
    token_manager.get_token.side_effect = Exception("boom")
    minio_client.check_bucket.side_effect = Exception("boom")

    body = client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert body["salesforce_connected"] is False
    assert body["minio_connected"] is False


def test_health_falsy_token_counts_as_disconnected(client, token_manager):
    token_manager.get_token.return_value = None

    body = client.get("/api/health").json()

    assert body["salesforce_connected"] is False
    assert body["status"] == "degraded"


def test_health_does_not_leak_exception_details(client, token_manager, minio_client):
    token_manager.get_token.side_effect = Exception("secret-consumer-key-abc123")
    minio_client.check_bucket.side_effect = Exception("minio-access-key-xyz789")

    resp = client.get("/api/health")

    assert "secret-consumer-key-abc123" not in resp.text
    assert "minio-access-key-xyz789" not in resp.text


def test_health_recovers_when_dependency_returns(client, token_manager):
    token_manager.get_token.side_effect = Exception("temporary")
    assert client.get("/api/health").json()["status"] == "degraded"

    token_manager.get_token.side_effect = None
    token_manager.get_token.return_value = ("tok", "https://x")
    assert client.get("/api/health").json()["status"] == "healthy"


@pytest.mark.xfail(
    reason="Route returns HTTP 200 with status='degraded'. The design doc (section 5.4) "
           "specifies 503 so Nomad's HTTP check marks the allocation unhealthy.",
    strict=False,
)
def test_health_degraded_returns_503(client, token_manager):
    token_manager.get_token.side_effect = Exception("boom")

    resp = client.get("/api/health")

    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"
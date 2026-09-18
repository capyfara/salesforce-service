import time
import jwt
import requests
from threading import Lock

class SalesforceTokenManager:
    """Manages OAuth 2.0 JWT Bearer tokens with automatic refresh."""

    def __init__(self, consumer_key, private_key_pem, username, login_url):
        self._consumer_key = consumer_key
        self._private_key = private_key_pem
        self._username = username
        self._login_url = login_url
        self._access_token = None
        self._instance_url = None
        self._expires_at = 0
        self._lock = Lock()

    def get_token(self) -> tuple[str, str]:
        """Returns (access_token, instance_url), refreshing if within 5 minutes of expiry."""
        with self._lock:
            if time.time() > (self._expires_at - 300):
                self._refresh()
        return self._access_token, self._instance_url

    def _refresh(self):
        now = int(time.time())
        claim = {
            "iss": self._consumer_key,
            "sub": self._username, 
            "aud": self._login_url,
            "exp": now + 180,
        }
        signed_jwt = jwt.encode(claim, self._private_key, algorithm="RS256")

        resp = requests.post(
            f"{self._login_url}/services/oauth2/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_jwt,
            },
            timeout=30,
        )

        resp = requests.post(
            f"{self._login_url}/services/oauth2/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_jwt,
            },
            timeout=30,
        )

        resp.raise_for_status()
        
        if not resp.ok:
            raise RuntimeError(
                f"Salesforce OAuth failed ({resp.status_code}): {resp.text}"
            )

        data = resp.json()

        self._access_token = data["access_token"]
        self._instance_url = data["instance_url"]
        self._expires_at = now + 7200  # tokens last ~2 hours
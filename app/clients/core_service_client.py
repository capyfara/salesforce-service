import hmac, hashlib, time, json, requests

class CoreServiceClient:
    def __init__(self, base_url: str, hmac_key: str, timeout: int = 30):
        self._base_url = base_url
        self._key = hmac_key
        self._timeout = timeout

    def _sign(self, body: str) -> dict:
        timestamp = str(time.time())
        signing_string = f"{timestamp}.{body}"
        signature = hmac.new(self._key.encode(), signing_string.encode(), hashlib.sha256).hexdigest()
        return {"X-Signature": signature, "X-Timestamp": timestamp, "X-Key-Id": "core"}

    def report_scan_status(self, scan_id: str, status: str, records: int) -> None:
        body = json.dumps({"status": status, "records": records})
        headers = {**self._sign(body), "Content-Type": "application/json"}
        resp = requests.patch(
            f"{self._base_url}/scans/{scan_id}", 
            data=body, headers=headers, 
            timeout=self._timeout
        )
        resp.raise_for_status()
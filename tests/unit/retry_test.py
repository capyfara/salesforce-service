import pytest
import requests
from clients.retry import RetryPolicy, with_retry

def http_error(status, body=None):
    r = requests.Response(); r.status_code = status
    r._content = (body or b"[]")
    return requests.HTTPError(response=r)

def test_recovers_from_rate_limit():
    calls, sleeps = {"n": 0}, []
    def flaky():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise http_error(403, b'[{"errorCode":"REQUEST_LIMIT_EXCEEDED"}]')
        return "ok"
    policy = RetryPolicy(base_delay=1, jitter=0, sleep=sleeps.append)

    assert with_retry(flaky, policy) == "ok"
    assert calls["n"] == 4
    assert sleeps == [1, 2, 4]                # exponential backoff

def test_gives_up_after_max_attempts():
    policy = RetryPolicy(max_attempts=3, sleep=lambda s: None)
    with pytest.raises(requests.HTTPError):
        with_retry(lambda: (_ for _ in ()).throw(http_error(503)), policy)

def test_does_not_retry_permanent_errors():
    calls = {"n": 0}
    def bad():
        calls["n"] += 1; raise http_error(400, b'[{"errorCode":"MALFORMED_QUERY"}]')
    with pytest.raises(requests.HTTPError):
        with_retry(bad, RetryPolicy(sleep=lambda s: None))
    assert calls["n"] == 1
import logging, random, time
from dataclasses import dataclass
from typing import Callable, Optional
import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_SF_CODES = {"REQUEST_LIMIT_EXCEEDED", "SERVER_UNAVAILABLE", "UNABLE_TO_LOCK_ROW"}


@dataclass
class RetryPolicy:
    max_attempts: int = 5
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.25                      # +/-25%
    sleep: Callable[[float], None] = time.sleep   # injectable so tests don't wait
    on_retry: Optional[Callable[[int, float, Exception], None]] = None


def _classify(exc: Exception, idempotent: bool):
    """Return (retryable, retry_after_seconds)."""
    if isinstance(exc, requests.ConnectionError):
        return True, None
    if isinstance(exc, requests.ReadTimeout):
        return idempotent, None               # request may have reached SF
    if isinstance(exc, requests.Timeout):
        return True, None
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        r = exc.response
        retry_after = r.headers.get("Retry-After")
        retry_after = float(retry_after) if retry_after and retry_after.isdigit() else None
        if r.status_code in RETRYABLE_STATUS:
            return True, retry_after
        try:
            body = r.json()
            codes = {e.get("errorCode") for e in body} if isinstance(body, list) else set()
        except ValueError:
            codes = set()
        if codes & RETRYABLE_SF_CODES:
            return True, retry_after
    return False, None


def with_retry(fn, policy: RetryPolicy, label: str = "", idempotent: bool = True):
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            retryable, retry_after = _classify(exc, idempotent)
            if not retryable or attempt >= policy.max_attempts:
                logger.error(f"[retry] {label} giving up after {attempt} attempt(s): {exc}")
                raise
            backoff = min(policy.max_delay, policy.base_delay * 2 ** (attempt - 1))
            backoff *= 1 + random.uniform(-policy.jitter, policy.jitter)
            delay = max(backoff, retry_after or 0)
            logger.warning(f"[retry] {label} attempt {attempt}/{policy.max_attempts} "
                           f"failed ({exc}); sleeping {delay:.1f}s")
            if policy.on_retry:
                policy.on_retry(attempt, delay, exc)
            policy.sleep(delay)
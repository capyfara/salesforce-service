# A tiny in-memory store -> swap for Postgres later without changing its interface:
from contextlib import contextmanager
import threading
from models.scan import ScanState

class InMemoryScanStore:
    def __init__(self):
        self._scans: dict[str, ScanState] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock() # protects _locks dict itself
    
    def _get_lock(self, scan_id: str) -> threading.Lock:
        with self._locks_guard:
            if scan_id not in self._locks:
                self._locks[scan_id] = threading.Lock()
            return self._locks[scan_id]
        
    @contextmanager
    def lock(self, scan_id: str):
        lock = self._get_lock(scan_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def create(self, scan: ScanState) -> None:
        self._scans[scan.scan_id] = scan
    
    def get(self, scan_id: str) -> ScanState | None:
        return self._scans.get(scan_id)
    
    def update(self, scan: ScanState) -> None:
        self._scans[scan.scan_id] = scan
    
    def list_all(self):
        return list(self._scans.values())
    
    def delete(self, scan_id: str) -> None:
        self._scans.pop(scan_id, None)
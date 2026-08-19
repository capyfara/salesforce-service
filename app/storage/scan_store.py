# A tiny in-memory store -> swap for Postgres later without changing its interface:
# Temporary before minIO setup
from models.scan import ScanState

class InMemoryScanStore:
    def __init__(self):
        self._scans: dict[str, ScanState] = {}

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
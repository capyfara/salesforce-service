# Remove duplicate records

class RecordDeduplicator:

    """
    Tracks seen Salesforce record Ids across pages within a single job,
    so no record is written to MinIO twice.
    """

    def __init__(self, id_field:str = "Id"):
        self._id_field = id_field
        self._seen_ids:set[str] = set()

    def deduplicate(self, records: list[dict]) -> list[dict]:
        unique_records = []
        for record in records:
            record_id = record.get(self._id_field)
            if record_id not in self._seen_ids or record_id is None:
                self._seen_ids.add(record_id)
                unique_records.append(record)
        return unique_records
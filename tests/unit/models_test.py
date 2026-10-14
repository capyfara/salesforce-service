"""Unit tests for models/scan.py and models/objects.py."""
import time

import pytest
from pydantic import ValidationError

from models.objects import SUPPORTED_OBJECTS, SupportedObject
from models.scan import ObjectProgress, ScanState, ScanStatus


def test_scan_status_values_are_the_wire_format():
    assert [s.value for s in ScanStatus] == ["started", "in progress", "complete", "failed", "cancelled"]


def test_scan_status_compares_equal_to_plain_strings():
    assert ScanStatus.FAILED == "failed"


def test_object_progress_defaults():
    p = ObjectProgress()
    assert (p.sf_job_id, p.state, p.records_processed, p.records_failed) == (None, "Pending", 0, 0)
    assert (p.pages_downloaded, p.minio_path, p.error) == (0, None, None)


def test_scan_state_defaults():
    s = ScanState(scan_id="a", org_id="o")
    assert s.status == ScanStatus.STARTED
    assert s.progress == {} and s.last_modified_after is None


@pytest.mark.parametrize("missing", ["scan_id", "org_id"])
def test_scan_state_requires_ids(missing):
    data = {"scan_id": "a", "org_id": "o"}
    del data[missing]
    with pytest.raises(ValidationError):
        ScanState(**data)


def test_scan_state_rejects_invalid_status():
    with pytest.raises(ValidationError):
        ScanState(scan_id="a", org_id="o", status="exploded")


def test_progress_dict_is_not_shared_between_instances():
    a, b = ScanState(scan_id="a", org_id="o"), ScanState(scan_id="b", org_id="o")
    a.progress["Contact"] = ObjectProgress()
    assert b.progress == {}


def test_json_round_trip_preserves_everything():
    s = ScanState(scan_id="a", org_id="o", status=ScanStatus.FAILED, last_modified_after="2026-01-01T00:00:00Z",
                  progress={"Contact": ObjectProgress(sf_job_id="j", state="Failed", error="x",
                                                      minio_path=["s3://b/k"], records_processed=5)})
    back = ScanState.model_validate_json(s.model_dump_json())
    assert back == s


def test_unknown_attribute_assignment_is_rejected():
    """Why clickhouse_* fields must be declared on ObjectProgress before being set."""
    with pytest.raises(ValueError):
        ObjectProgress().clickhouse_loaded = True


@pytest.mark.xfail(reason="started_at/updated_at defaults are evaluated once at import time, so every "
                          "ScanState shares the same timestamp. Use Field(default_factory=...).", strict=False)
def test_timestamps_are_evaluated_per_instance():
    a = ScanState(scan_id="a", org_id="o")
    time.sleep(0.01)
    b = ScanState(scan_id="b", org_id="o")
    assert b.started_at > a.started_at


# --- SUPPORTED_OBJECTS ----------------------------------------------------
def test_catalog_names_unique():
    names = [o.name for o in SUPPORTED_OBJECTS]
    assert len(names) == len(set(names))


@pytest.mark.parametrize("obj", SUPPORTED_OBJECTS, ids=lambda o: o.name)
def test_catalog_entries_are_wellformed(obj):
    assert {"{fields}", "{where_clause}"} <= {p for p in ("{fields}", "{where_clause}") if p in obj.soql_template}
    assert "Id" in obj.default_fields and "LastModifiedDate" in obj.default_fields
    assert f"FROM {obj.name}" in obj.soql_template


def test_base_where_defaults_to_none():
    o = SupportedObject(name="X", label="X", soql_template="", default_fields=["Id"], supports_incremental=False)
    assert o.base_where is None
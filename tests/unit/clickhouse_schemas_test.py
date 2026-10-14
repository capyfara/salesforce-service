"""Unit tests for models/clickhouse_schemas.py (DDL + INSERT ... SELECT s3() generation)."""
import pytest

from models import clickhouse_schemas as cs
from models.objects import SUPPORTED_OBJECTS, SupportedObject


def spec(name):
    return cs.SCHEMAS[name]


def kinds(name):
    return dict(spec(name).columns)


# --- registry -------------------------------------------------------------
def test_one_schema_per_supported_object():
    assert set(cs.SCHEMAS) == {o.name for o in SUPPORTED_OBJECTS}


@pytest.mark.parametrize("obj", SUPPORTED_OBJECTS, ids=lambda o: o.name)
def test_table_name_matches_minio_prefix(obj):
    """upload_records writes under object_name.lower(); the s3() glob depends on it."""
    assert cs.SCHEMAS[obj.name].table == obj.name.lower()


@pytest.mark.parametrize("obj", SUPPORTED_OBJECTS, ids=lambda o: o.name)
def test_columns_follow_default_fields_in_order(obj):
    assert [n for n, _ in cs.SCHEMAS[obj.name].columns] == obj.default_fields


@pytest.mark.parametrize("obj", SUPPORTED_OBJECTS, ids=lambda o: o.name)
def test_every_column_kind_is_known(obj):
    assert all(k in cs._KINDS for _, k in cs.SCHEMAS[obj.name].columns)


def test_all_field_kinds_overrides_reference_real_kinds():
    assert set(cs.FIELD_KINDS.values()) <= set(cs._KINDS)


# --- kind assignment ------------------------------------------------------
def test_known_fields_get_specific_kinds():
    k = kinds("Opportunity")
    assert k["Id"] == "id"
    assert k["LastModifiedDate"] == "version"
    assert k["CreatedDate"] == "datetime"
    assert k["CloseDate"] == "date"
    assert k["Amount"] == "decimal"


def test_unlisted_fields_default_to_string():
    assert kinds("Contact")["Email"] == "string"
    assert kinds("Account")["BillingCity"] == "string"


# --- _build validation ----------------------------------------------------
def make_obj(fields):
    return SupportedObject(name="Thing", label="Things", soql_template="SELECT {fields} FROM Thing {where_clause}",
                           default_fields=fields, supports_incremental=True)


@pytest.mark.parametrize("fields,missing", [
    (["Name", "LastModifiedDate"], "Id"),
    (["Id", "Name"], "LastModifiedDate"),
])
def test_build_requires_id_and_last_modified(fields, missing):
    with pytest.raises(ValueError, match=missing):
        cs._build(make_obj(fields))


def test_build_rejects_unknown_kind(monkeypatch):
    monkeypatch.setitem(cs.FIELD_KINDS, "Weird__c", "no-such-kind")
    with pytest.raises(ValueError, match="unknown kind"):
        cs._build(make_obj(["Id", "LastModifiedDate", "Weird__c"]))


def test_build_lowercases_table_name():
    assert cs._build(make_obj(["Id", "LastModifiedDate"])).table == "thing"


# --- DDL ------------------------------------------------------------------
def test_ddl_is_idempotent_and_db_qualified():
    ddl = spec("Contact").ddl("salesforce")
    assert "CREATE TABLE IF NOT EXISTS salesforce.contact" in ddl


def test_ddl_engine_and_sort_key():
    ddl = spec("Contact").ddl("db")
    assert "ReplacingMergeTree(LastModifiedDate)" in ddl
    assert "ORDER BY (org_id, Id)" in ddl


def test_ddl_adds_bookkeeping_columns():
    ddl = spec("Contact").ddl("db")
    assert "org_id LowCardinality(String)" in ddl
    assert "scan_id String" in ddl
    assert "_ingested_at DateTime DEFAULT now()" in ddl


def test_ddl_version_and_id_columns_are_not_nullable():
    ddl = spec("Contact").ddl("db")
    assert "`Id` String," in ddl
    assert "`LastModifiedDate` DateTime64(3, 'UTC')," in ddl
    assert "Nullable(DateTime64(3, 'UTC'))" in ddl      # CreatedDate stays nullable


def test_ddl_types_by_kind():
    ddl = spec("Opportunity").ddl("db")
    assert "`Amount` Nullable(Decimal(18, 2))" in ddl
    assert "`CloseDate` Nullable(Date)" in ddl
    assert "`Name` Nullable(String)" in ddl


@pytest.mark.parametrize("obj", SUPPORTED_OBJECTS, ids=lambda o: o.name)
def test_ddl_includes_every_field(obj):
    ddl = cs.SCHEMAS[obj.name].ddl("db")
    assert all(f"`{f}`" in ddl for f in obj.default_fields)


# --- INSERT ... SELECT ----------------------------------------------------
def test_insert_reads_parquet_via_s3_with_named_params():
    sql = spec("Contact").insert_sql("salesforce")
    assert "INSERT INTO salesforce.contact" in sql
    assert "s3(%(url)s, %(access_key)s, %(secret_key)s, 'Parquet')" in sql
    assert "%(org_id)s, %(scan_id)s" in sql


def test_insert_column_list_matches_select_expression_count():
    s = spec("Account")
    sql = s.insert_sql("db")
    cols_part = sql.split("(org_id, scan_id,")[1].split(")")[0]
    assert cols_part.count("`") // 2 == len(s.columns)


def test_insert_casts_each_kind():
    sql = spec("Opportunity").insert_sql("db")
    assert "nullIf(`Name`, '')" in sql
    assert "parseDateTime64BestEffortOrNull(`CreatedDate`, 3, 'UTC')" in sql
    assert "parseDateTime64BestEffortOrZero(`LastModifiedDate`, 3, 'UTC')" in sql
    assert "toDateOrNull(`CloseDate`)" in sql
    assert "toDecimal64OrNull(`Amount`, 2)" in sql


def test_insert_leaves_id_uncast():
    assert ", `Id`," in spec("Contact").insert_sql("db").replace("%(scan_id)s, `Id`", ", `Id`")


def test_bool_kind_maps_empty_to_null_and_true_string():
    _, expr = cs._KINDS["bool"]
    rendered = expr.format(c="`IsActive`")
    assert "= ''" in rendered and "lower(`IsActive`) = 'true'" in rendered


def test_spec_is_immutable():
    with pytest.raises(Exception):
        spec("Contact").table = "other"
"""
Source for how each Salesforce object is represented in ClickHouse.

This module:
  1. Derives one table definition per object in SUPPORTED_OBJECTS, using each
     object's `default_fields` (so adding an object to models/objects.py
     automatically gives it a table; no second list to keep in sync).
  2. Generates the CREATE TABLE DDL (used by ClickHouseClient.ensure_schema()).
  3. Generates the INSERT ... SELECT FROM s3(...) statement that reads the
     Parquet files from MinIO and casts each string column to its real type
     (used by ClickHouseClient.load_object()).
"""

from dataclasses import dataclass

from models.objects import SUPPORTED_OBJECTS

# ---------------------------------------------------------------------------
# Type catalogue
# ---------------------------------------------------------------------------
# kind -> (ClickHouse column type, SELECT expression template).
# "{c}" is replaced with the backtick-quoted source column from the Parquet file.
_KINDS: dict[str, tuple[str, str]] = {
    # Salesforce record Id. Never empty, so not Nullable (it is part of ORDER BY).
    "id": ("String", "{c}"),

    # Plain text. Salesforce CSV exports empty values as '', so map '' -> NULL.
    "string": ("Nullable(String)", "nullIf({c}, '')"),

    # Timestamps such as 2026-01-15T08:00:00.000+0000.
    "datetime": (
        "Nullable(DateTime64(3, 'UTC'))",
        "parseDateTime64BestEffortOrNull({c}, 3, 'UTC')",
    ),

    # Same as datetime, but NOT Nullable: ReplacingMergeTree requires its
    # version column to be non-null. Unparseable values become 1970-01-01.
    "version": (
        "DateTime64(3, 'UTC')",
        "parseDateTime64BestEffortOrZero({c}, 3, 'UTC')",
    ),

    # Calendar dates such as 2026-04-30.
    "date": ("Nullable(Date)", "toDateOrNull({c})"),

    # Currency/amounts. Decimal avoids floating-point rounding in sums.
    "decimal": ("Nullable(Decimal(18, 2))", "toDecimal64OrNull({c}, 2)"),

    # Salesforce booleans arrive as the strings 'true' / 'false'.
    "bool": ("Nullable(Bool)", "if({c} = '', NULL, lower({c}) = 'true')"),
}

# ---------------------------------------------------------------------------
# Field -> kind overrides
# ---------------------------------------------------------------------------
# Any Salesforce field NOT listed here is loaded as Nullable(String).
FIELD_KINDS: dict[str, str] = {
    "Id": "id",
    "LastModifiedDate": "version",   # version column for ReplacingMergeTree

    "CreatedDate": "datetime",
    "SystemModstamp": "datetime",
    "LastLoginDate": "datetime",
    "StartDateTime": "datetime",     # Event
    "EndDateTime": "datetime",       # Event

    "CloseDate": "date",             # Opportunity
    "ActivityDate": "date",          # Task / Event
    "LastActivityDate": "date",
    "ConvertedDate": "date",         # Lead

    "Amount": "decimal",             # Opportunity
    "AnnualRevenue": "decimal",      # Account / Lead

    "IsActive": "bool",              # User
    "IsConverted": "bool",           # Lead
    "IsDeleted": "bool",
}

# Columns every table must contain: the engine and sort key depend on them.
_REQUIRED_FIELDS = {"Id", "LastModifiedDate"}


# ---------------------------------------------------------------------------
# Table specification
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TableSpec:
    """Describes one ClickHouse table and how to load it from Parquet."""

    table: str
    columns: tuple[tuple[str, str], ...]   # (salesforce_field, kind)

    def ddl(self, db: str) -> str:
        """CREATE TABLE statement. Idempotent (IF NOT EXISTS)."""
        cols = ",\n    ".join(f"`{n}` {_KINDS[k][0]}" for n, k in self.columns)
        return f"""
            CREATE TABLE IF NOT EXISTS {db}.{self.table} (
                org_id LowCardinality(String),
                scan_id String,
                {cols},
                _ingested_at DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(LastModifiedDate)
            ORDER BY (org_id, Id)
        """

    def insert_sql(self, db: str) -> str:
        """
        INSERT ... SELECT that reads Parquet from MinIO through ClickHouse's
        s3() table function and casts every column to its real type.

        Named parameters (bound by clickhouse-connect):
          org_id, scan_id   - stamped onto every row
          url               - s3 URL or glob, e.g. .../account/page_*.parquet
          access_key, secret_key - MinIO credentials
        """
        names = ", ".join(f"`{n}`" for n, _ in self.columns)
        exprs = ", ".join(_KINDS[k][1].format(c=f"`{n}`") for n, k in self.columns)
        return f"""
            INSERT INTO {db}.{self.table} (org_id, scan_id, {names})
            SELECT %(org_id)s, %(scan_id)s, {exprs}
            FROM s3(%(url)s, %(access_key)s, %(secret_key)s, 'Parquet')
        """


# ---------------------------------------------------------------------------
# Schema registry (built from SUPPORTED_OBJECTS)
# ---------------------------------------------------------------------------
def _build(obj) -> TableSpec:
    """Build a TableSpec from a SupportedObject's default_fields."""
    missing = _REQUIRED_FIELDS - set(obj.default_fields)
    if missing:
        raise ValueError(
            f"{obj.name}.default_fields is missing required field(s) "
            f"{sorted(missing)}; the ClickHouse table needs them for its "
            f"ReplacingMergeTree version column and sort key."
        )

    unknown_kinds = {
        f: FIELD_KINDS[f] for f in obj.default_fields
        if FIELD_KINDS.get(f, "string") not in _KINDS
    }
    if unknown_kinds:
        raise ValueError(f"{obj.name}: unknown kind(s) in FIELD_KINDS: {unknown_kinds}")

    return TableSpec(
        table=obj.name.lower(),   # must match the MinIO prefix used by upload_records
        columns=tuple((f, FIELD_KINDS.get(f, "string")) for f in obj.default_fields),
    )


# object name (e.g. "Contact") -> TableSpec. Used by ClickHouseClient.
SCHEMAS: dict[str, TableSpec] = {o.name: _build(o) for o in SUPPORTED_OBJECTS}
from dataclasses import dataclass

# kind -> (ClickHouse type, SELECT expression template)
_KINDS = {
    "id":       ("String",                    "{c}"),
    "string":   ("Nullable(String)",          "nullIf({c}, '')"),
    "datetime": ("Nullable(DateTime64(3, 'UTC'))", "parseDateTime64BestEffortOrNull({c}, 3, 'UTC')"),
    "version":  ("DateTime64(3, 'UTC')",      "parseDateTime64BestEffortOrZero({c}, 3, 'UTC')"),
    "date":     ("Nullable(Date)",            "toDateOrNull({c})"),
    "decimal":  ("Nullable(Decimal(18, 2))",  "toDecimal64OrNull({c}, 2)"),
}

@dataclass(frozen=True)
class TableSpec:
    table: str
    columns: list[tuple[str, str]]   # (salesforce_field, kind)

    def ddl(self, db: str) -> str:
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
        names = ", ".join(f"`{n}`" for n, _ in self.columns)
        exprs = ", ".join(_KINDS[k][1].format(c=f"`{n}`") for n, k in self.columns)
        return f"""
INSERT INTO {db}.{self.table} (org_id, scan_id, {names})
SELECT %(org_id)s, %(scan_id)s, {exprs}
FROM s3(%(url)s, %(access_key)s, %(secret_key)s, 'Parquet')
"""

SCHEMAS: dict[str, TableSpec] = {
    "Contact": TableSpec("contact", [
        ("Id", "id"), ("Name", "string"), ("Email", "string"), ("Phone", "string"),
        ("AccountId", "string"), ("OwnerId", "string"),
        ("CreatedDate", "datetime"), ("LastModifiedDate", "version"),
    ]),
    "Account": TableSpec("account", [
        ("Id", "id"), ("Name", "string"), ("Type", "string"), ("Industry", "string"),
        ("BillingCity", "string"), ("BillingState", "string"), ("OwnerId", "string"),
        ("CreatedDate", "datetime"), ("LastModifiedDate", "version"),
    ]),
    "Opportunity": TableSpec("opportunity", [
        ("Id", "id"), ("Name", "string"), ("AccountId", "string"), ("OwnerId", "string"),
        ("StageName", "string"), ("Amount", "decimal"), ("CloseDate", "date"),
        ("CreatedDate", "datetime"), ("LastModifiedDate", "version"),
    ]),
}
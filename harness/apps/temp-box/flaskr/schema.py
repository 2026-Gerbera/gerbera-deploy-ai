"""Read-only schema inspection; a version row alone is not readiness."""

import hashlib
import json

from sqlalchemy import DateTime, Integer, String, Text, inspect, select, text

from .db import metadata, schema_version

EXPECTED = "0001"


class SchemaError(ValueError):
    """Fixed-message validation failure, safe to return to callers."""


def type_name(value):
    if isinstance(value, Integer):
        return "integer"
    if isinstance(value, DateTime):
        return "datetime"
    if isinstance(value, Text):
        return "text"
    if isinstance(value, String):
        return f"string:{value.length}"
    return "unsupported"


def inspect_schema(connection):
    inspector = inspect(connection)
    tables = sorted(inspector.get_table_names())
    shape = {}
    for name in tables:
        shape[name] = {
            "columns": sorted(
                (col["name"], type_name(col["type"]), bool(col["nullable"]))
                for col in inspector.get_columns(name)
            ),
            "defaults": {
                col["name"]: str(col["default"]).lower().replace("(", "").replace(")", "")
                for col in inspector.get_columns(name)
                if col["default"] is not None
            },
            "pk": inspector.get_pk_constraint(name)["constrained_columns"],
            "unique": sorted(
                sorted(item["column_names"]) for item in inspector.get_unique_constraints(name)
            ),
            "fk": sorted(
                (item["constrained_columns"], item["referred_table"], item["referred_columns"])
                for item in inspector.get_foreign_keys(name)
            ),
        }
    encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()
    signature = "sha256:" + hashlib.sha256(encoded).hexdigest()
    current = None
    if "schema_version" in tables:
        versions = connection.execute(select(schema_version.c.version)).scalars().all()
        if len(versions) != 1:
            raise SchemaError("Invalid schema version records")
        current = versions[0]
    return current, signature, shape


def validate_schema(connection, allow_empty=False):
    current, signature, shape = inspect_schema(connection)
    if not shape and allow_empty:
        return current, signature
    if current != EXPECTED or set(shape) != set(metadata.tables):
        raise SchemaError("Schema version or tables do not match")
    for name, table in metadata.tables.items():
        expected_columns = sorted(
            (col.name, type_name(col.type), col.nullable) for col in table.columns
        )
        actual = shape[name]
        if actual["columns"] != expected_columns:
            raise SchemaError("Schema columns do not match")
        if actual["pk"] != [col.name for col in table.primary_key]:
            raise SchemaError("Schema primary key does not match")
    if shape["post"]["defaults"] != {"created": "current_timestamp"}:
        raise SchemaError("Schema timestamp default does not match")
    if shape["user"]["unique"] != [["username"]]:
        raise SchemaError("Schema username uniqueness does not match")
    if shape["post"]["fk"] != [(["author_id"], "user", ["id"])]:
        raise SchemaError("Schema author foreign key does not match")
    # Reads also verify the application account can access all required columns.
    for table in metadata.tables.values():
        connection.execute(select(table).limit(0))
    return current, signature


def fingerprint(connection):
    if connection.dialect.name == "sqlite":
        return {
            "version": str(connection.execute(text("SELECT sqlite_version()")).scalar_one()),
            "sql_mode": "",
            "collation": "BINARY",
            "time_zone": "UTC",
            "ssl_version": "",
        }
    values = connection.execute(
        text("SELECT VERSION(), @@SESSION.sql_mode, @@collation_database, @@SESSION.time_zone")
    ).one()
    ssl = connection.execute(text("SHOW SESSION STATUS LIKE 'Ssl_version'")).one()
    return dict(
        zip(
            ("version", "sql_mode", "collation", "time_zone", "ssl_version"),
            (str(value or "") for value in (*values, ssl[1])),
            strict=True,
        )
    )

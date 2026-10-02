"""Explicit non-destructive migration: python -m flaskr.migrate PHASE --json."""

import argparse
import hashlib
import json
import sys

from .config import database_url
from .db import make_engine, metadata, schema_version
from .schema import EXPECTED, fingerprint, inspect_schema, validate_schema


def run(phase):
    result = {
        "phase": phase,
        "ok": False,
        "current": None,
        "expected": EXPECTED,
        "applied": [],
        "signature": "sha256:" + hashlib.sha256(b"{}").hexdigest(),
        "fingerprint": dict.fromkeys(
            ("version", "sql_mode", "collation", "time_zone", "ssl_version"), ""
        ),
    }
    engine = None
    try:
        engine = make_engine(database_url(migration=True))
        with engine.begin() as connection:
            result["fingerprint"] = fingerprint(connection)
            current, signature, shape = inspect_schema(connection)
            result.update(current=current, signature=signature)
            if phase == "up" and not shape:
                metadata.create_all(connection)
                connection.execute(schema_version.insert().values(version=EXPECTED))
                result["applied"] = [EXPECTED]
            current, signature = validate_schema(connection, allow_empty=phase == "precheck")
            result.update(current=current, signature=signature)
        result["ok"] = True
    except Exception:
        # Driver errors may contain credentials, DSNs, SQL or parameter values.
        result["error"] = "migration_failed"
    finally:
        if engine is not None:
            engine.dispose()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("precheck", "up", "verify"))
    parser.add_argument(
        "--json", action="store_true", help="Emit one JSON object (also the default)"
    )
    args = parser.parse_args()
    result = run(args.phase)
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

"""source=runtime-fixture: named volume 기록으로 one-off 단계/지속성을 검증한다."""

import hashlib
import json
import sys
from pathlib import Path

phase = sys.argv[1]
path = Path("/data/applied.json")
applied = json.loads(path.read_text()) if path.exists() else []
changed = phase == "up" and "001_fixture" not in applied
if changed:
    applied.append("001_fixture")
    path.write_text(json.dumps(applied))
sys.stdout.write(
    "MIGRATE_RESULT "
    + json.dumps(
        {
            "phase": phase,
            "ok": True,
            "current": "001_fixture" if applied else "0000",
            "expected": "001_fixture",
            "signature": "sha256:" + hashlib.sha256(b"runtime-fixture-schema").hexdigest(),
            "fingerprint": {
                "version": "fixture",
                "sql_mode": "",
                "collation": "fixture",
                "time_zone": "UTC",
                "ssl_version": "",
            },
            "applied": ["001_fixture"] if changed else [],
        }
    )
    + "\n"
)

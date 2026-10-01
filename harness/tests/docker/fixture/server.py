"""테스트 전용 HTTP 앱. source=runtime-fixture; O3 Flask 앱과 별개다."""

import json
import os
import re
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = json.dumps(
            {
                "source": "runtime-fixture",
                "version": os.environ["FIXTURE_VERSION"],
                "release_id": os.environ.get("RELEASE_ID"),
                "secret_present": "SECRET_KEY" in os.environ,
                "app_base_url": os.environ.get("APP_BASE_URL"),
                "secret_valid": bool(
                    re.fullmatch(r"[0-9a-f]{64}", os.environ.get("SECRET_KEY", ""))
                ),
            }
        ).encode()
        self.send_response(
            503
            if self.path == "/health/ready" and os.environ.get("FIXTURE_READY") == "fail"
            else 200
        )
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


def stop(signum, frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
with HTTPServer(("0.0.0.0", 8000), Handler) as server:  # noqa: S104 - isolated container
    server.serve_forever()

"""파이프라인 확인용 HTTP 앱. 외부 패키지, DB, 비밀값이 필요 없다."""

from __future__ import annotations

import json
import os
import signal
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONTENT = json.loads(Path(__file__).with_name("content.json").read_text(encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        status, content_type = 200, "application/json; charset=utf-8"
        if path == "/":
            content_type = "text/html; charset=utf-8"
            title, color = escape(CONTENT["title"]), escape(CONTENT["color"], quote=True)
            body = (
                '<!doctype html><html lang="ko"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f"<title>{title}</title><style>"
                "body{font:24px system-ui;display:grid;place-items:center;min-height:90vh;"
                f"background:{color};color:white}}main{{text-align:center}}</style>"
                f"<main><h1>{title}</h1><p>코드에서 서버까지, 배포 완료.</p>"
                '<p><a href="/version" style="color:inherit">릴리스 확인</a></p></main></html>'
            )
        elif path == "/version":
            body = json.dumps({"release_id": os.environ.get("RELEASE_ID", "local")})
        elif path == "/health/ready":
            body = json.dumps({"ready": True})
        else:
            status, body = 404, '{"error":"not_found"}'
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        # readiness polling으로 로그를 채우지 않는다.
        pass


def stop(signum: int, frame: object) -> None:
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    with ThreadingHTTPServer(("0.0.0.0", 8000), Handler) as server:  # noqa: S104 - container HTTP
        server.serve_forever()

"""온프렘 관리 웹/감시와 현재 HEAD 요청·잠금 해제. 비밀값은 인자로 받지 않는다."""

import argparse
import http.cookiejar
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from ddak.core.config import Settings
from ddak.core.redact import redact


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("run", "plan", "unlock"))
    parser.add_argument("--project", default=os.environ.get("DDAK_WATCH_PROJECT") or "flaskr")
    parser.add_argument("--ref")
    args = parser.parse_args()
    if args.action == "run":
        import uvicorn

        from ddak.app import ADMIN_HOST, create

        defaults = {
            "DDAK_ADAPTER_MODE": "real",
            "DDAK_BUILD_BACKEND": "local",
            "DDAK_IMAGE_REPOSITORY": "2026gerbera/flaskr",
            "DDAK_JEV_BACKEND": "claude-cli",
            "DDAK_LLM_BACKEND": "cli",
        }
        settings = Settings.from_env({**defaults, **os.environ})
        uvicorn.run(
            create(cli_host=ADMIN_HOST, onprem_profile=True, settings=settings),
            host=ADMIN_HOST,
            port=settings.admin_port,
        )
        return
    settings = Settings.from_env()
    base = f"http://127.0.0.1:{settings.admin_port}"
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    opener.open(base + "/ops?" + urlencode({"project": args.project}), timeout=10).close()
    csrf = next(cookie.value for cookie in jar if cookie.name == "ddak_csrf")
    data = {"project": args.project, "csrf_token": csrf}
    if args.ref:
        data["ref"] = args.ref
    if args.action == "unlock":
        data["reason"] = "로컬 운영 CLI에서 대상 상태 확인 후 제품 차단 해제"
    request = Request(  # noqa: S310 - loopback HTTP 상수와 고정 action만 사용
        base + "/ops/" + args.action, data=urlencode(data).encode(), headers={"Origin": base}
    )
    with opener.open(request, timeout=600) as response:
        print(json.dumps(json.load(response), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read(8192)).get("detail", "운영 요청 실패")
        except (ValueError, AttributeError):
            detail = "운영 요청 실패; 관리 페이지 실행 기록 확인"
        print(f"HTTP {exc.code}: {redact(str(detail))}", file=sys.stderr)
        raise SystemExit(1) from None
    except (URLError, StopIteration):
        print(
            "로컬 관리 페이지에 연결할 수 없습니다. onprem-run 상태를 확인하세요.", file=sys.stderr
        )
        raise SystemExit(1) from None

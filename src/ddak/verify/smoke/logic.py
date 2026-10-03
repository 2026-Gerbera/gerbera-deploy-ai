"""smoke_test 공통 로직: HTTP 클라이언트, 시나리오, 판정. 환경 차이는 어댑터가 준다.

- 같은 시나리오를 두 환경에 독립적으로 보낸다(세션·쿠키를 환경 간에 재사용하지 않는다).
- 리다이렉트는 따라가지 않고 상태 코드와 Location 경로를 그대로 본다. 다른 호스트로 보내면
  경로 대신 EXTERNAL_LOCATION으로 남겨 실패로 본다(호스트 이름은 결과에 싣지 않는다).
- 앱 응답은 신뢰하지 않는 입력이다. 본문 크기(MAX_BODY), JSON 중첩 깊이(MAX_JSON_DEPTH),
  요청 하나의 전체 시간(소켓 연산마다가 아니라 요청 전체), smoke 실행 전체 시간(ctx.deadline,
  없으면 SMOKE_TOTAL_S)에
  상한을 둔다. DNS 조회는 표준 라이브러리 소켓 timeout 밖이라 이 상한에 들어가지 않는다.
- 검사 불합격은 passed=False로 돌려준다. 주소가 없는 등 수행할 수 없으면 DdakToolError.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.client
import json
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from typing import Any, Literal, Protocol
from urllib.parse import urlencode, urlsplit

from ddak.core.adapters import TargetAdapter
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.smoke_test import (
    Observed,
    SmokeScenario,
    SmokeTestInput,
    SmokeTestOutput,
)
from ddak.core.smoke import V2_BOX_MARK
from ddak.core.storage import STORAGE_SMOKE_GROUP
from ddak.verify.smoke.normalize import own_post, page_head

MAX_BODY = 256 * 1024
MAX_JSON_DEPTH = 32
REQUEST_TIMEOUT_S = 10.0  # 요청 하나(연결·전송·응답 전체)
# smoke 실행 전체. 실행기는 카탈로그 시간(registry smoke_test 120초)으로 ctx.deadline을 준다.
# 그 값을 줄이지 않는다. ctx.deadline이 없을 때(수동 호출·테스트)만 이 값을 쓴다
SMOKE_TOTAL_S = 120.0
SESSION_COOKIE = "session"
EXTERNAL_LOCATION = "<external>"


@dataclass(frozen=True)
class Response:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: str
    raw_body: bytes | None = None

    def header(self, name: str) -> str | None:
        values = self.all(name)
        return values[0] if values else None

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]


@dataclass(frozen=True)
class ImageUpload:
    data: bytes


Form = Mapping[str, str] | ImageUpload

UPLOAD_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgaPj/HwAEggJ/59habAAAAABJRU5ErkJggg=="
)


class HttpClient(Protocol):
    def request(self, method: str, path: str, form: Form | None, timeout: float) -> Response: ...


class SmokeAdapter(TargetAdapter, Protocol):
    @property
    def name(self) -> str: ...

    @property
    def source(self) -> Literal["live", "fixture"]: ...

    def client(self, ctx: RunContext) -> HttpClient: ...


class UrlClient:
    """표준 라이브러리 HTTP 클라이언트. https는 인증서·호스트명을 검증한다(끄지 않는다)."""

    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "스모크 대상 주소 형식 오류")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "스모크 대상 주소 형식 오류")
        self._https = parts.scheme == "https"
        self._host = parts.hostname
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self.host = parts.hostname.lower()  # 리다이렉트 호스트 비교용

    def request(self, method: str, path: str, form: Form | None, timeout: float) -> Response:
        """timeout은 요청 전체(연결·전송·응답 헤더·본문) 시간이다.

        소켓 timeout은 연산 한 번마다라서 1바이트씩 천천히 보내는 응답은 끝없이 길어질 수 있다.
        그래서 timeout이 지나면 감시 타이머가 소켓을 닫고 TimeoutError를 낸다.
        """
        conn: http.client.HTTPConnection
        if self._https:
            conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        headers = {"User-Agent": "ddak-smoke/1", "Accept": "*/*"}
        body = None
        if isinstance(form, ImageUpload):
            boundary = "ddak-smoke-image-boundary"
            body = (
                (
                    f"--{boundary}\r\n"
                    'Content-Disposition: form-data; name="image"; filename="smoke.png"\r\n'
                    "Content-Type: image/png\r\n\r\n"
                ).encode()
                + form.data
                + f"\r\n--{boundary}--\r\n".encode()
            )
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif form is not None:
            body = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        expired = threading.Event()
        # 연결 직후 소켓을 잡아 둔다. 응답이 연결을 닫는 형식이면 getresponse()가 conn.sock을
        # 비우고 소켓을 응답 쪽으로 넘기므로, 그때 conn.sock으로는 끊을 수 없다
        held: list[socket.socket] = []

        def abort() -> None:
            expired.set()
            for sock in held:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

        watchdog = threading.Timer(timeout, abort)
        watchdog.daemon = True
        resp: http.client.HTTPResponse | None = None
        try:
            watchdog.start()
            conn.connect()  # 소켓 timeout으로 묶인다
            if conn.sock is not None:
                held.append(conn.sock)
            if expired.is_set():  # 연결하는 사이에 시간이 다 됐으면 바로 끊는다
                abort()
            conn.request(method, self._prefix + path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(MAX_BODY + 1)[:MAX_BODY]
        except (OSError, http.client.HTTPException):
            if expired.is_set():
                raise TimeoutError("요청 전체 시간 초과") from None
            raise
        finally:
            watchdog.cancel()
            if resp is not None:
                resp.close()
            conn.close()
        if expired.is_set():  # 소켓을 닫으면 read가 잘린 본문을 오류 없이 돌려줄 수 있다
            raise TimeoutError("요청 전체 시간 초과")
        return Response(
            resp.status, tuple(resp.getheaders()), data.decode("utf-8", "replace"), data
        )


# ---- 시나리오 -----------------------------------------------------------------


@dataclass
class Probe:
    """한 번의 smoke 실행 상태. 시나리오 함수가 공유한다."""

    client: HttpClient
    ctx: RunContext
    title: str  # 이번 run이 만든 글 제목(퀴즈 문구 + 익명 tag, smoke_post)
    body: str  # 이번 run이 만든 글 본문(정답·맞장구, run 고유값 없음)
    deadline: float  # smoke 실행 전체 마감(monotonic). ctx.deadline, 없으면 시작 + SMOKE_TOTAL_S
    host: str | None = (
        None  # 대상 호스트(리다이렉트 비교용). 모르면 절대 주소 Location은 외부로 본다
    )

    def timeout(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise DdakToolError(ErrorCode.ADAPTER_TIMEOUT, "스모크 제한 시간 초과")
        return min(REQUEST_TIMEOUT_S, remaining)

    def get(self, path: str) -> Response:
        return self.client.request("GET", path, None, self.timeout())

    def post(self, path: str, form: Mapping[str, str]) -> Response:
        return self.client.request("POST", path, form, self.timeout())


Check = Callable[[Probe], SmokeScenario]


def _json_depth_ok(text: str) -> bool:
    """문자열 밖 [·{ 중첩이 MAX_JSON_DEPTH 이하인지. json.loads의 RecursionError를 막는다."""
    depth, in_string, escaped = 0, False, False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                return False
        elif ch in "]}":
            depth -= 1
    return True


def _json(resp: Response) -> dict[str, Any]:
    if not _json_depth_ok(resp.body):
        raise ValueError("JSON 중첩이 너무 깊다")
    value = json.loads(resp.body)
    if not isinstance(value, dict):
        raise ValueError("JSON 객체가 아니다")
    return value


def _location_path(resp: Response, host: str | None) -> str | None:
    """Location의 경로. 다른 호스트(또는 호스트를 모르는 절대 주소)면 EXTERNAL_LOCATION."""
    location = resp.header("Location")
    if location is None:
        return None
    parts = urlsplit(location.strip())
    if parts.scheme or parts.netloc:  # //다른호스트/ 도 netloc이 있다
        target = (parts.hostname or "").lower()
        if host is None or target != host:
            return EXTERNAL_LOCATION
    return parts.path or "/"


def cookie_attrs(resp: Response) -> dict[str, Observed]:
    """Set-Cookie에서 세션 쿠키의 이름·속성만 뽑는다. 값은 버린다."""
    for raw in resp.all("Set-Cookie"):
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:  # 형식이 깨진 헤더는 이름만 본다
            if raw.split("=", 1)[0].strip() == SESSION_COOKIE:
                return {"cookie": SESSION_COOKIE}
            continue
        morsel = jar.get(SESSION_COOKIE)
        if morsel is not None:
            return {
                "cookie": SESSION_COOKIE,
                "httponly": bool(morsel["httponly"]),
                "secure": bool(morsel["secure"]),
                "samesite": str(morsel["samesite"] or ""),
                "path": str(morsel["path"] or ""),
            }
    return {"cookie": None}


def _scalar(value: object) -> Observed:
    """앱 응답은 신뢰하지 않는 입력이다. 짧은 스칼라로만 남긴다."""
    if value is None or isinstance(value, bool | int):
        return value
    return str(value)[:128]


def _scenario(
    sid: str, ok: bool, resp: Response | None, detail: str, normalized: Mapping[str, object]
) -> SmokeScenario:
    return SmokeScenario(
        id=sid,
        ok=ok,
        status=resp.status if resp is not None and 100 <= resp.status <= 599 else None,
        detail="" if ok else detail[:200],
        normalized={key: _scalar(value) for key, value in normalized.items()},
    )


def s0_version(p: Probe) -> SmokeScenario:
    resp = p.get("/version")
    data = _json(resp) if resp.status == 200 else {}
    db = data.get("db") if isinstance(data.get("db"), dict) else {}
    normalized: dict[str, Observed] = {
        "release_id": data.get("release_id"),
        "schema_expected": data.get("schema_expected"),
        "app_env": data.get("app_env"),
        "db.dialect": db.get("dialect"),
        "db.tls": db.get("tls"),
        "db.tls_verified": db.get("tls_verified"),
    }
    problems = []
    if resp.status != 200:
        problems.append(f"/version 상태 {resp.status}")
    if data.get("release_id") != p.ctx.run_id:
        problems.append("release_id가 이번 run과 다름")
    if db.get("dialect") != "mysql":
        problems.append("DB가 mysql이 아님")
    return _scenario("S0.version", not problems, resp, ", ".join(problems), normalized)


# 클라우드 health_check(cloud/health/health.py)와 같은 준비 판정: HTTP 200이고
# status가 ok·ready이거나 ready가 true. 같은 배포에서 헬스만 통과하고 스모크가 떨어지지 않게 한다
READY_STATUSES = frozenset({"ok", "ready"})


def s0_ready(p: Probe) -> SmokeScenario:
    resp = p.get("/health/ready")
    data = _json(resp) if resp.status in (200, 503) else {}
    schema = data.get("schema") if isinstance(data.get("schema"), dict) else {}
    normalized: dict[str, Observed] = {
        "status": data.get("status"),
        "schema.current": schema.get("current"),
        "schema.expected": schema.get("expected"),
    }
    status = data.get("status")  # 신뢰하지 않는 값: 문자열일 때만 비교한다
    ok = resp.status == 200 and (
        (isinstance(status, str) and status in READY_STATUSES) or data.get("ready") is True
    )
    return _scenario("S0.ready", ok, resp, f"/health/ready 상태 {resp.status}", normalized)


def b1_index(p: Probe) -> SmokeScenario:
    resp = p.get("/")
    cookie = cookie_attrs(resp)
    ok = resp.status == 200 and "Posts" in resp.body
    return _scenario("B1", ok, resp, f"목록 상태 {resp.status}", {"status": resp.status, **cookie})


# B2.create가 남기는 글. 게시판에 계속 쌓이므로 테스트 표시 대신 실제 글처럼 보이는 넌센스
# 퀴즈·한마디를 쓴다. 문구에 & < > " ' 를 넣지 않는다(템플릿 이스케이프로 바뀌면 목록 반영 판정의
# 원문 비교가 깨진다). 가족 친화적인 내용만, 실존 인물·브랜드는 넣지 않는다.
# (제목 앞부분, 정답) → 본문 "정답: …"
_KO_QUIZ: tuple[tuple[str, str], ...] = (
    ("세상에서 가장 빠른 닭은?", "후다닭"),
    ("아몬드가 죽으면?", "다이아몬드"),
    ("깨가 죽으면?", "주근깨"),
    ("물고기 중 학벌이 제일 좋은 물고기는?", "고등어"),
    ("세상에서 가장 착한 사자는?", "자원봉사자"),
    ("진짜 새의 이름은?", "참새"),
    ("왕이 궁에 들어가기 싫을 때 하는 말은?", "궁시렁궁시렁"),
    ("소금의 유통기한은?", "천일"),
    ("칼이 정색하면?", "검정색"),
    ("세상에서 가장 지루한 중학교는?", "로딩중"),
    ("펭귄이 다니는 중학교는?", "냉방중"),
    ("세상에서 가장 뜨거운 과일은?", "천도복숭아"),
    ("오리가 얼면?", "언덕"),
    ("소가 웃으면?", "우하하"),
    ("미소의 반대말은?", "당기소"),
    ("반성문을 영어로 하면?", "글로벌"),
    ("사과가 웃으면?", "풋사과"),
    ("개 중에 가장 아름다운 개는?", "무지개"),
    ("토끼가 쓰는 빗은?", "래빗"),
    ("세상에서 가장 쉬운 숫자는?", "십구만"),
    ("다리미가 좋아하는 음식은?", "피자"),
    ("세상에서 가장 가난한 왕은?", "최저임금"),
)
# (한마디, 맞장구) → 본문은 맞장구 그대로
_KO_LINE: tuple[tuple[str, str], ...] = (
    ("다들 오늘도 수고 많으셨어요", "덕분에 힘이 나요"),
    ("퇴근까지 조금만 더 힘내요", "같이 힘내요"),
    ("창밖 하늘이 참 맑네요", "산책하기 딱 좋은 날이에요"),
    ("주말에 뭐 하세요?", "푹 쉬는 게 최고예요"),
)
# (제목 앞부분, 答え) → 본문 "答え: …"
_JA_QUIZ: tuple[tuple[str, str], ...] = (
    ("パンはパンでも食べられないパンは?", "フライパン"),
    ("逆立ちすると軽くなる動物は?", "イルカ"),
    ("食べると安心するケーキは?", "ホットケーキ"),
    ("冷蔵庫の中にいる大きな動物は?", "ゾウ"),
    ("切っても切っても切れないものは?", "水"),
    ("上は大水、下は大火事、なーんだ?", "お風呂"),
)
# (다쟈레·한마디, 맞장구) → 본문은 맞장구 그대로
_JA_LINE: tuple[tuple[str, str], ...] = (
    ("アルミ缶の上にあるミカン", "思わず笑っちゃいました"),
    ("布団が吹っ飛んだ", "今日も平和ですね"),
    ("電話に誰も出んわ", "あとでかけ直してみます"),
    ("イクラはいくら", "お寿司が食べたくなりました"),
    ("内容がないよう", "それもまた良しです"),
    ("カエルが帰る", "気をつけて帰ってね"),
    ("校長先生が絶好調", "今日も元気ですね"),
    ("ダジャレを言うのは誰じゃ", "ちょっと寒いけど好きです"),
    ("猫が寝転んだ", "かわいいですね"),
    ("アイスを愛す", "わかります"),
)
# (제목 앞부분, 본문, 익명 표시)
SMOKE_POSTS: tuple[tuple[str, str, str], ...] = (
    *((q, f"정답: {a}", "익명") for q, a in _KO_QUIZ),
    *((line, reply, "익명") for line, reply in _KO_LINE),
    *((q, f"答え: {a}", "匿名") for q, a in _JA_QUIZ),
    *((line, reply, "匿名") for line, reply in _JA_LINE),
)


def smoke_post(run_id: str) -> tuple[str, str]:
    """run_id로 고른 (제목, 본문). 같은 run_id면 두 환경이 같은 글을 써서 화면 지문이 맞는다.

    sha256 앞 8자리로 풀 항목을 고르고, 다음 6자리를 익명 tag로 제목에 붙인다. 제목에 run
    고유값이 들어가야 목록 반영 판정(제목 원문 포함)과 normalize의 smoke 제목 치환이 이전 글과
    섞이지 않는다. 본문에는 run 고유값을 넣지 않는다.
    """
    digest = hashlib.sha256(run_id.encode()).hexdigest()
    text, body, handle = SMOKE_POSTS[int(digest[:8], 16) % len(SMOKE_POSTS)]
    return f"{text} · {handle} {digest[8:14]}", body


def b2_create(p: Probe) -> SmokeScenario:
    resp = p.post("/create", {"title": p.title, "body": p.body})
    location = _location_path(resp, p.host)
    listed = p.get("/") if resp.status == 302 else None
    shown = listed is not None and listed.status == 200 and p.title in listed.body
    ok = resp.status == 302 and location == "/" and shown
    detail = f"글쓰기 상태 {resp.status}, 이동 {location}, 목록 반영 {shown}"
    # 정규화 비교(compare가 두 환경 값을 맞춰 본다). 판정(ok)에는 쓰지 않는다
    body = listed.body if listed is not None and listed.status == 200 else ""
    return _scenario(
        "B2.create",
        ok,
        resp,
        detail,
        {
            "status": resp.status,
            "location": location,
            "listed": shown,
            **cookie_attrs(resp),
            "page.head": page_head(body, smoke_title=p.title) if shown else None,
            "page.post": own_post(body, p.title) if shown else None,
        },
    )


def b2_empty_title(p: Probe) -> SmokeScenario:
    resp = p.post("/create", {"title": "", "body": "smoke"})
    shown = "Title is required." in resp.body
    ok = resp.status == 200 and shown
    return _scenario(
        "B2.empty",
        ok,
        resp,
        f"빈 제목 상태 {resp.status}, 오류 문구 {shown}",
        {"status": resp.status, "error_shown": shown},
    )


# S13 대체(로그인이 없어 51자 아이디 대신 글 제목). post.title은 VARCHAR(200)이다. 앱이 길이를
# 검사하지 않으면 엄격 sql_mode(온프렘)는 1406 오류, 비엄격(RDS 설정에 따라)은 잘린 채 저장된다
LONG_TITLE_LEN = 201


def b2_long_title(p: Probe) -> SmokeScenario:
    resp = p.post("/create", {"title": "x" * LONG_TITLE_LEN, "body": "smoke"})
    shown = "Title is too long." in resp.body
    ok = resp.status == 200 and shown
    return _scenario(
        "B2.long",
        ok,
        resp,
        f"긴 제목 상태 {resp.status}, 오류 문구 {shown}",
        {"status": resp.status, "error_shown": shown},
    )


# v2(앱 태그 v2, 1차 데모): 목록 페이지에 이미지(인라인 SVG)와 박스를 추가했다. 템플릿만 바뀐다.
V2_BOX_TEXT = "v2: image and box added"


def v2_box(p: Probe) -> SmokeScenario:
    """목록 페이지에 v2 박스와 그 안의 이미지가 보이는지. v1에서는 실패가 정상이다."""
    resp = p.get("/")
    start = resp.body.find(V2_BOX_MARK)
    section = resp.body[start : resp.body.find("</section>", start)] if start >= 0 else ""
    box = start >= 0 and V2_BOX_TEXT in section
    image = "<svg" in section
    ok = resp.status == 200 and box and image
    return _scenario(
        "V2.box",
        ok,
        resp,
        f"목록 상태 {resp.status}, 박스 {box}, 이미지 {image}",
        {"status": resp.status, "box": box, "image": image},
    )


class _UploadImages(HTMLParser):
    def __init__(self, text: str):
        super().__init__()
        self.paths: set[str] = set()
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            src = dict(attrs).get("src") or ""
            if re.fullmatch(r"/uploads/[0-9]+\.(?:png|jpg|gif)", src):
                self.paths.add(src)


def storage_upload(p: Probe) -> SmokeScenario:
    before = p.get("/uploads")
    if before.status != 200:
        return _scenario("V3.upload", False, before, "업로드 목록 조회 실패", {})
    uploaded = p.client.request("POST", "/upload", ImageUpload(UPLOAD_PNG), p.timeout())
    if uploaded.status != 302 or _location_path(uploaded, p.host) != "/uploads":
        return _scenario("V3.upload", False, uploaded, "이미지 업로드 또는 이동 경로 실패", {})
    after = p.get("/uploads")
    created = _UploadImages(after.body).paths - _UploadImages(before.body).paths
    if after.status != 200 or len(created) != 1:
        return _scenario("V3.upload", False, after, "새 업로드 파일 한 개를 확인할 수 없다", {})
    path = next(iter(created))
    expected = hashlib.sha256(UPLOAD_PNG).hexdigest()
    responses = [p.get(path) for _ in range(6)]
    matched = sum(
        r.status == 200
        and r.raw_body is not None
        and hashlib.sha256(r.raw_body).hexdigest() == expected
        for r in responses
    )
    return _scenario(
        "V3.upload",
        matched == 6,
        uploaded,
        f"업로드 조회 해시 일치 {matched}/6",
        {"reads": 6, "matched": matched, "sha256": expected},
    )


# 시나리오 묶음. plan step params의 scenarios가 이 이름을 고른다. (id, 검사) 순서대로 실행한다.
# base = v1 익명 게시판(B2는 익명 글쓰기가 되는 동안만 유효). v2 = 이미지·박스(base에 더해 고른다:
# ["base", "v2"]). v1으로 되돌린 run은 base만 고른다.
GROUPS: dict[str, tuple[tuple[str, Check], ...]] = {
    "base": (
        ("S0.version", s0_version),
        ("S0.ready", s0_ready),
        ("B1", b1_index),
        ("B2.create", b2_create),
        ("B2.empty", b2_empty_title),
        ("B2.long", b2_long_title),
    ),
    "v2": (("V2.box", v2_box),),
    STORAGE_SMOKE_GROUP: (("V3.upload", storage_upload),),
}


def _run_check(sid: str, check: Check, probe: Probe) -> SmokeScenario:
    try:
        return check(probe)
    except DdakToolError:
        raise
    except (OSError, http.client.HTTPException) as error:
        return _scenario(sid, False, None, f"연결 실패: {type(error).__name__}", {})
    except (ValueError, RecursionError) as error:  # JSON 형식 오류 등(깊이 검사를 지나친 경우 대비)
        return _scenario(sid, False, None, f"응답 형식 오류: {type(error).__name__}", {})


def run_smoke(adapter: SmokeAdapter, inp: SmokeTestInput, ctx: RunContext) -> SmokeTestOutput:
    unknown = [g for g in inp.scenarios if g not in GROUPS]
    if unknown:
        raise DdakToolError(ErrorCode.CONFIG_INVALID, f"알 수 없는 스모크 시나리오 묶음: {unknown}")
    started = time.monotonic()
    deadline = ctx.deadline if ctx.deadline is not None else started + SMOKE_TOTAL_S
    client = adapter.client(ctx)
    title, body = smoke_post(ctx.run_id)
    probe = Probe(
        client=client,
        ctx=ctx,
        title=title,
        body=body,
        deadline=deadline,
        host=getattr(client, "host", None),
    )
    checks = dict(item for g in inp.scenarios for item in GROUPS[g])  # 같은 검사는 한 번만
    results = [_run_check(sid, check, probe) for sid, check in checks.items()]
    return SmokeTestOutput(
        run_id=inp.run_id,
        target=inp.target,
        passed=bool(results) and all(r.ok for r in results),
        elapsed_s=round(time.monotonic() - started, 3),
        scenarios=results,
        source=adapter.source,
    )

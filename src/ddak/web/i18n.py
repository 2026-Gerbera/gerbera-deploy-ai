"""요청 언어와 표시 단계 번역. 저장된 판정 데이터·SSE·JSON 원문은 바꾸지 않는다."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

from fastapi import Request, Response
from fastapi.templating import Jinja2Templates
from jinja2 import BaseLoader, Environment, FileSystemLoader, pass_context

from ddak.core.logging import get_logger
from ddak.web.translations_ja import translate, translate_static

LANG_COOKIE = "ddak_lang"
_log = get_logger("web.i18n")
# 문자열 안의 닫는 구분자도 코드로 남긴다. Jinja 주석 역시 번역하지 않는다.
_JINJA = re.compile(
    r"({{(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|(?:(?!}}).))*}}"
    r"|{%(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|(?:(?!%}).))*%}"
    r"|{#.*?#})",
    re.S,
)


def language(request: Request) -> str:
    try:
        query = request.query_params.get("lang")
        if query in ("ko", "ja"):
            return query
        cookie = request.cookies.get(LANG_COOKIE)
        return cookie if cookie in ("ko", "ja") else "ko"
    except Exception:
        _log.warning("표시 언어 판정 실패: 한국어 사용")
        return "ko"


def language_url(request: Request, selected: str) -> str:
    if request.method == "GET":
        path, query = request.url.path, request.url.query
    else:
        from ddak.web.form_errors import form_return_to

        path, _, query = form_return_to(request).partition("?")
    # 승인·진행 별칭도 동일한 HTML을 제공한다.
    path = re.sub(r"^/ops(/runs/[^/]+/approval)$", r"\1", path)
    if re.fullmatch(r"/runs/[^/]+", path):
        path += "/progress"
    items = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != "lang"]
    return path + "?" + urlencode([*items, ("lang", selected)])


def translate_tree(value: Any) -> Any:
    if type(value) is str:
        return translate(value)
    if isinstance(value, Mapping):
        return {key: translate_tree(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [translate_tree(item) for item in value]
    return value


@pass_context
def localize_json(context, value):
    return translate_tree(value) if context.get("ui_language") == "ja" else value


def finalize_ja(value: Any) -> Any:
    # Markup과 매크로 HTML은 문자열로 바꾸지 않는다.
    return translate(value) if type(value) is str else value


class JapaneseLoader(BaseLoader):
    def __init__(self, directory: Path):
        self.loader = FileSystemLoader(directory)

    def get_source(self, environment: Environment, template: str):
        source, filename, current = self.loader.get_source(environment, template)
        pieces = _JINJA.split(source)
        return (
            "".join(
                part if index % 2 else translate_static(part) for index, part in enumerate(pieces)
            ),
            filename,
            current,
        )


class LocalizedTemplates:
    """기존 라우트와 같은 TemplateResponse 인터페이스를 제공한다."""

    def __init__(self, directory: Path, *, context_processors: list):
        self.ko = Jinja2Templates(directory=directory, context_processors=context_processors)
        self.ja = Jinja2Templates(
            env=Environment(
                loader=JapaneseLoader(directory), autoescape=True, finalize=finalize_ja
            ),
            context_processors=context_processors,
        )
        self.env = self.ko.env
        self.env.globals.update(
            localize_json=localize_json,
            ui_language="ko",
            language_links={"ko": "/?lang=ko", "ja": "/?lang=ja"},
            app_wording={},
        )

    def get_template(self, name: str):
        """요청 없이 쓰는 기존 한국어 템플릿 API를 유지한다."""
        return self.ko.get_template(name)

    def TemplateResponse(self, *args, **kwargs):
        request = kwargs.get("request") or (args[0] if args else None)
        selected = language(request)
        context = dict(kwargs.get("context") or {})
        try:
            return self._render(selected, request, context, args, kwargs)
        except Exception:
            if selected != "ja":
                raise
            _log.warning("일본어 화면 렌더 실패: 한국어로 표시")
            # 실패한 렌더가 소비한 오류 표시 플래그를 복원한다.
            request.state.form_error_shown = False
            return self._render("ko", request, context, args, kwargs)

    def _render(self, selected, request, context, args, kwargs):
        from ddak.web.js_wording import wording

        engine = self.ja if selected == "ja" else self.ko
        if selected == "ja":
            engine.env.globals.update(self.env.globals)
            engine.env.filters.update(self.env.filters)
            engine.env.policies.update(self.env.policies)
        context = {
            **context,
            "ui_language": selected,
            "language_links": {lang: language_url(request, lang) for lang in ("ko", "ja")},
            "app_wording": wording(selected) if selected == "ja" else {},
        }
        return engine.TemplateResponse(*args, **{**kwargs, "context": context})


class LanguageMiddleware:
    """ASGI 송신 헤더만 감싼다. 이벤트 스트림 본문은 버퍼링하지 않는다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from ddak.core.answer_language import request_language

        request = Request(scope)
        selected = language(request)
        token = request_language.set(selected)
        try:
            set_cookie = request.query_params.get("lang") in ("ko", "ja")
        except Exception:
            set_cookie = False

        async def send_language(message):
            if message["type"] == "http.response.start" and set_cookie:
                cookie = Response()
                cookie.set_cookie(
                    LANG_COOKIE,
                    selected,
                    max_age=30 * 24 * 60 * 60,
                    httponly=True,
                    samesite="strict",
                    path="/",
                    secure=False,
                )  # security.issue_csrf와 같은 로컬 HTTP 조건.
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        *(header for header in cookie.raw_headers if header[0] == b"set-cookie"),
                    ],
                }
            await send(message)

        try:
            await self.app(scope, receive, send_language)
        finally:
            request_language.reset(token)


def remember_run_language(request: Request) -> None:
    """CSRF 검사 뒤 실행을 만드는 요청의 답변 언어만 저장한다."""
    if request.url.path not in {"/ops/plan", "/settings/deploy"} and not re.fullmatch(
        r"/ops/demo/(?:reset-v1|prepare-v2|prepare-v3)", request.url.path
    ):
        return
    from ddak.web.dependencies import deployment, selected_project

    service = deployment(request)
    if not hasattr(service, "save_project_settings") or not hasattr(
        service, "get_project_settings"
    ):
        return
    form = getattr(request.state, "submitted_form", {})
    project = selected_project(request, form.get("project"))
    saved = service.get_project_settings(project) or {}
    selected = language(request)
    if saved.get("ai_answer_language", "ko") != selected:
        service.save_project_settings(
            project,
            {"ai_answer_language": selected},
            updated_by="local-operator",
            expected_version=saved.get("version", 0),
        )

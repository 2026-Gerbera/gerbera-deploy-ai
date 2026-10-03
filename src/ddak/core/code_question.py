"""관리 웹 '질문' 버튼의 서버 쪽. 배포 없이 앱 저장소 소스로 코드 질문에 답한다.

- 기준 소스: 프로젝트 설정의 저장소 URL·감시 브랜치 최신 커밋. 제품 전용 앱 checkout
  (AppRepository)을 재사용하고 fetch 뒤 커밋을 git 읽기 명령으로만 본다(작업 트리 변경 없음).
- 파일 선택·비밀 경로 제외·크기 상한은 ddak.core.code_context(코드)가 정한다.
- AI 호출은 레지스트리 툴 answer_code_question이 한다. 이 모듈은 툴을 import하지 않고
  조립부(ddak.app)가 넘긴 answer 함수로만 부른다(계약 4).
- 읽기 전용: 배포·패치·커밋·승인을 만들지 않는다.
"""

from __future__ import annotations

import re
import secrets
import threading
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ddak.core.app_repository import FakeAppRepository, git_sha
from ddak.core.code_context import collect_context
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.answer_code_question import (
    MAX_QUESTION_CHARS,
    AnswerCodeQuestionInput,
    AnswerCodeQuestionOutput,
)
from ddak.core.logging import get_logger
from ddak.core.redact import redact

Answer = Callable[[AnswerCodeQuestionInput, RunContext, Settings], AnswerCodeQuestionOutput]

_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
_log = get_logger(__name__)


class CodeQuestion:
    def __init__(self, service: Any, settings: Settings, *, answer: Answer) -> None:
        self.service = service
        self.settings = settings
        self._answer = answer
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    def ask(self, project: str, question: str) -> dict[str, Any]:
        question = question.strip()
        if not question:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "질문을 입력하세요")
        if len(question) > MAX_QUESTION_CHARS:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, f"질문은 {MAX_QUESTION_CHARS}자 이하로 입력하세요"
            )
        with self._lock:
            if project in self._busy:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "이전 질문의 답을 만드는 중입니다. 잠시 뒤 다시 시도하세요",
                )
            self._busy.add(project)
        try:
            return self._ask(project, question)
        finally:
            with self._lock:
                self._busy.discard(project)

    def _ask(self, project: str, question: str) -> dict[str, Any]:
        saved = dict(self.service.get_project_settings(project) or {})
        repo_url = saved.get("repo_url")
        if not repo_url:
            raise DdakToolError(
                ErrorCode.CONFIG_INVALID, "프로젝트 설정에 앱 저장소 URL이 없습니다"
            )
        branch = str(saved.get("watch_branch") or "prod").removeprefix("refs/heads/")
        if not _BRANCH.fullmatch(branch) or ".." in branch or branch.endswith((".lock", "/")):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "감시 브랜치 이름 형식 오류")
        settings = self.settings
        onboarding = getattr(self.service, "onboarding", None)
        if onboarding is not None:
            settings = onboarding.effective(project, saved, onboarding.vault)
        run_id = "qa-" + secrets.token_hex(8)
        ctx = RunContext(
            run_id=run_id,
            project=project,
            adapter_mode=settings.adapter_mode,
            repo_url=repo_url,
            ref=branch,
            project_settings=saved,
        )
        repository = self.service.connect_repository(ctx)
        if repository is None:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "앱 저장소 연결이 필요합니다")
        if isinstance(repository, FakeAppRepository):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED,
                "가짜 실행 모드 저장소에서는 코드 질문을 쓸 수 없습니다",
            )
        # 원격 추적 ref만 갱신한다. checkout·merge·작업 트리 변경은 없다.
        repository.git(
            "fetch", "--no-tags", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"
        )
        sha = git_sha(
            repository.git("rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}")
        )
        context = collect_context(repository.git_bytes, sha, question)
        inp = AnswerCodeQuestionInput(
            run_id=run_id,
            question=question,
            commit=sha,
            branch=branch,
            files=context.files,
            paths=context.paths,
        )
        out = self._answer(inp, replace(ctx, source_sha=sha), settings)
        if out.commit != sha:
            raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "답의 기준 커밋이 요청과 다르다")
        _log.info(
            "코드 질문 답변",
            project=project,
            commit=sha[:7],
            files=len(context.files),
            context_bytes=context.total_bytes,
            skipped_secret=context.skipped_secret,
        )
        return {
            "answer": redact(out.answer, max_len=None),
            "sources": list(out.sources),
            "commit": sha[:7],
            "branch": branch,
            "files": len(context.files),
            "truncated": context.truncated,
            "source": out.source.value if out.source is not None else None,
        }

"""대시보드 '질문' 버튼: 배포 없이 앱 저장소 소스로 코드 질문에 답한다(JSON, fetch 전용).

웹은 AI·툴을 import하지 않는다(계약 1·4). 조립부가 붙인 service.code_question만 부른다.
답은 브라우저에서 textContent로만 그린다(HTML·마크다운 해석 없음).
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.logging import get_logger
from ddak.web.dependencies import deployment, selected_project
from ddak.web.form_errors import error_data
from ddak.web.forms import parse_form
from ddak.web.security import require_safe_post

router = APIRouter(prefix="/code-qa")
_log = get_logger(__name__)


@router.post("/ask")
async def ask(request: Request) -> JSONResponse:
    try:
        form = await parse_form(request)
        require_safe_post(request, form.get("csrf_token", ""))
        project = selected_project(request, form.get("project"))
        service = getattr(deployment(request), "code_question", None)
        if service is None:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "코드 질문 서비스가 연결되지 않았습니다"
            )
        result = await run_in_threadpool(service.ask, project, form.get("question", ""))
    except Exception as exc:  # 오류도 같은 답 박스에 보여 준다. 원문 예외는 내보내지 않는다
        status, error = error_data(exc, {})
        if isinstance(exc, DdakToolError) and exc.code is ErrorCode.CONFIG_INVALID:
            status = 400
        elif not isinstance(exc, DdakToolError | HTTPException | ValueError | KeyError):
            _log.warning("코드 질문 처리 실패", error=type(exc).__name__)
        return JSONResponse({"ok": False, "error": error}, status_code=status)
    return JSONResponse({"ok": True, **result})

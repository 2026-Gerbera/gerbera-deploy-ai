"""관리 웹 라우터 자리(양서윤 C3). 기존 web/app.py의 create_app이 지금 동작을 유지한다.

라우터를 여기로 나누면 app.py에서 include_router로 붙인다. 툴 모듈을 import하지 않는다(계약 4).
"""

from ddak.web.routes import approvals, code_qa, events, pages, patches, results, settings

ROUTERS = (
    pages.router,
    settings.router,
    approvals.router,
    patches.router,
    events.router,
    results.router,
    code_qa.router,
)

__all__ = ["ROUTERS"]

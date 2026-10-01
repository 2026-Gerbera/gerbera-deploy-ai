# plan/planner (담당: 김준석)
- 할 일: `generate_plan`. AI는 OPTIONAL step의 포함 여부 `(id, include, reason)`만 제안한다.
- 흐름: 결정 가능 step 없음 -> AI 미호출 / Jev(`ask_jev`, 확률>=0.5 포함) -> Claude(`call_ai`, `prompt.md`, `plan-v1`) -> 빈 초안(`fallback=True`).
- `feedback`은 Claude 호출의 데이터 구역에만 들어간다. Jev 경로는 무시한다.
- 입출력: `core/contracts/tools/generate_plan.py`. 툴 등록은 `tool.py`.
- 다른 디렉토리는 `__init__.py` 공개 이름으로만 import. `plan/validate`는 import 금지.

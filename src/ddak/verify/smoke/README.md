# verify/smoke (담당: 장민영)
- 할 일: 스모크 테스트(smoke_test). 배포한 앱에 같은 시나리오를 환경마다 독립적으로 보낸다. 테스트 데이터(`[smoke <run_id>]` 글)만 쓴다
- 입출력 계약: `src/ddak/core/contracts/tools/smoke_test.py`(C-10 초안: `passed`, `elapsed_s`, `scenarios[]`, `source`)
- 구성: `logic.py`(HTTP·시나리오·판정), `local.py`(인벤토리 `public_url` 또는 tier `APP_BASE_URL`), `cloud.py`(`https://<cloud_domain>`, 인증서 검증), `fake.py`(v1 응답 흉내, `source=fixture`), `tool.py`(등록)
- 시나리오 묶음(`scenarios` params): `base` = S0.version(release_id = run_id, DB mysql), S0.ready, B1 목록, B2.create 익명 글쓰기(302 → `/`, 목록 반영), B2.empty 빈 제목 오류. v2 기능 묶음은 기능이 정해지면 `GROUPS`에 추가한다
- 결과에 쿠키 값·주소·비밀값을 넣지 않는다(쿠키는 이름·HttpOnly·Secure·SameSite·Path만)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).

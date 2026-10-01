# verify/compare (담당: 장민영)
- 할 일: 교차 검증(compare_env_results)
- 현재: 비교 로직만 `logic.py`에 순수 함수로 있다(`compare_env`). **툴 등록은 아직이다.** 실행기가 두 환경의 smoke 결과·DB 서명을 compare에 넘기는 방법(RunContext 확장 또는 run 기록 파일)이 O1과 정해지면 tool.py와 입출력 모델을 만든다.
- 비교 대상(P0): smoke 결과(시나리오별 ok·status·normalized), 이미지(두 환경의 실제 digest가 같은 index에 속하는지), 스키마 서명(S10, 있을 때만)
- 판정: match / mismatch / expected_diff / skipped. 실패는 mismatch가 하나라도 있거나 match가 하나도 없을 때
- 예상된 차이(코드 고정 목록 `EXPECTED_DIFF_KEYS`): app_env, db.tls, db.tls_verified, 쿠키 secure, 플랫폼 digest(arm64·amd64). 보고에는 예상된 차이의 값을 싣지 않는다
- 입출력 계약: 툴 등록 때 `src/ddak/core/contracts/tools`에 만든다(골든패스 8-5 초안: passed, unexpected_diffs, checks[])
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).

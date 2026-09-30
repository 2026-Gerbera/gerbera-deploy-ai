# Third-Party Notices

현재 이 저장소에 **복사한 외부 코드는 없습니다.**

- devpi-guardian(MIT)은 규칙과 문서 구조만 참고했고 코드는 새로 작성했습니다([docs/harness/05](docs/harness/05_devpi-guardian-참고.md)).
- 툴 레지스트리의 `@tool` 데코레이터는 LangChain과 비슷한 모양이지만 LangChain 코드·패키지를 쓰지 않고 pydantic으로 새로 작성했습니다.
- 해커톤 이전 작업물의 재사용 허용 여부가 확인되지 않았으므로(I-22), 당분간 우리 팀의 이전 코드는 구조와 아이디어만 가져옵니다.
- **추가 예정:** 샘플 앱 베이스 flaskr(pallets/flask `examples/tutorial`, BSD-3-Clause, ✅ 골든 패스 결정). `apps/sample-app`에 복사하는 PR에서 아래 형식으로 원본 LICENSE 전문과 출처 커밋 SHA를 남깁니다(O3 장민영).

## 추가할 때의 형식

외부 코드나 테스트를 상당 부분 복사하면 해당 파일 머리 주석과 이 파일에 모두 남깁니다.

```
### <프로젝트 이름>
- 출처: <저장소 URL>@<커밋 SHA>
- 라이선스: <SPDX 식별자, 예: MIT>
- 복사한 파일: <우리 저장소 경로> ← <원본 경로>
- 원 저작권 고지와 허가 문구(전문):
  <원본 LICENSE 전문을 그대로 붙임>
```

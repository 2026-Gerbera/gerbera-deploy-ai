# 수정11 — AI 코드 패치·관리자 설정

기준 main a6da5bd(PR #17), worktree `.worktrees/fix11`, branch `o1/fix11-ai-patch`. 다른 worktree·실환경·commit/push/PR은 작업 대상이 아니다. 사용자 10/3 지시 A→B(+C) 순서다. PR #16(main 971c8e6)을 통합했다. 아래 초기 계약 이후의 최종 정합 절을 우선한다.

## A 계약

- `PatchTarget(file,line,pattern_id,is_new,severity,key)` 및 Facts/AnalyzeProjectOutput.patch_targets: 위치/패턴/키만, 원문값 없음. EnvKey.required 선택 필드 기본 True, 선택 read 및 제품 RELEASE_ID/SOURCE_SHA는 False.
- 패턴 정의는 core.patch_patterns에 둔다. 판단 AI에는 대상 위치와 이름만 전달한다. 편집 의도는 코드가 원본에서 diff로 렌더한다. 검사 실패 시 임의 수정·AI 실행 도구 호출은 없다.
- 설정의 code_patch 기본 True. OFF는 새 제안을 끄며 동일 원본의 이전 성공 패치 재사용을 끄지 않는다. 원본이 바뀌어 기존 패치를 유지할 수 없으면 손실 관문에서 중단한다. 이전 'OFF면 prod 그대로' 가정은 이 결정으로 폐기한다.
- 원장은 환경별 성공 릴리스에 파일별 원본/결과/diff 해시·제거 줄 해시만 기록한다. 재사용 diff는 run의 0700 patches 디렉토리·0600 파일. 실패 릴리스는 재사용하지 않는다. 동일 원본 파일만 자동 재사용하고 결과 해시를 대조한다.
- 이월 이미지는 원본 이미지의 tier_tree_hash가 현재 승인 트리의 해당 tier 입력과 같아야 한다. 증거가 없으면 재빌드한다. 과거 기록을 새 승인 트리의 증거로 꾸미지 않는다.
- A 패치 메타의 passed/patch_sha256/new_env_keys/gitleaks는 승인 전 검사와 결합한다. 임의 사용자 meta만으로 외부 검사 생략을 허용하지 않는다. 기존 approved tree/candidate SHA/digest 승인 계약을 유지한다.
- 원본에 있는 발견을 제거하는 패치는 최종 수정본 검사를 통과할 수 있다. 원본의 fingerprint 예외는 변경된 파일에 적용하지 않는다. 새로운 비밀값·남은 비밀값은 차단한다.

## 담당자 확인 필요

- 준석: analyze/flow/validate의 patch_targets·required 소비, 설정별 AI 선택 연결.
- 민영: PR #16의 생성기와 편집 의도 JSON 경계 통합; deterministic intents renderer 및 shared pattern 검사.
- 서윤: AI 코드 수정 토글·검사 결과·값 필요 표시 및 뒤이은 설정/연결 화면.

## 검토

adversarial-quality-gate의 Pre Mencius 5개 지적(값 없는 의도, 필수 읽기, 성공 원장, 이월 트리, 소스 검사/도구 준비 순서)을 구현·회귀 기준으로 채택했다. TDD는 사용하지 않는다. 사용자 금지에 따라 외부 Claude 검토/호출은 실행하지 않는다. 독립 Post 검토와 검증 결과는 인계서와 ai-usage 완료 로그에 기록한다.

## 10/3 최종 구현 정합

- PR16(main 971c8e6)을 통합했고 815ea60은 diff를 참고해 겹치는 검사기 수정만 반영했다. 생성은 값 없는 EditIntent를 받아 코드 렌더한다. 새 제안 불합격/연결 실패는 경고와 함께 폐기하고 원본/성공 원장으로 진행한다. AI_NOT_ALLOWED와 재사용 손실 관문은 계속 중단한다.
- 관리자 화면에는 검사 패턴·대상 해시·필수 키 이름만 전달한다. diff 원문은 0600 private 파일이며 patch_sha256 승인 결합은 유지한다.
- O2 범위는 10/3 사용자 결정으로 O1에 포함한다. 위 '준석 확인 필요'는 변경 공유 목록으로 대체한다. 민영·서윤의 동시 변경은 인계서의 merge 주의 목록을 따른다.
- Settings/프로젝트 설정에 선택적 generation/judgment provider/model, effort, build backend/image repository/inventory path를 추가한다. RunContext에는 required_env_keys만 이름으로 넣고 platform.controller_tools에 관리형 도구 경로를 결합한다. CodeBuild 실행에서도 같은 scanner 경로를 사용한다.
- provider 등록부에서 cli/api 종류·역할·인증·모델·factory/status/test를 고른다. api.py는 인터페이스이며 Groq 구현은 groq_api.py로 이동, 기존 api backend는 Groq 호환을 유지한다. Claude API는 별도 선택이다. TypeSafe Jev는 미연결 예약이며 키 전송 경로가 없다.
- 관리형 빌드 도구 설치와 DB/WEB/WAS 준비는 공개 계획 해시 승인 뒤 제품 코드가 실행한다. 환경 값·키·토큰은 0600 저장/전용 stdin으로 다루고 화면/AI/로그로 반환하지 않는다. 준비 성공은 환경 current/previous나 배포 릴리스 성공 기록을 바꾸지 않는다.
- 기존 DB 연결 등록은 runtime DATABASE_URL과 별도 migration.env URL 입력으로 분리한다. 신규 계정은 승인 뒤 암호를 생성한다. 기존 계정 암호를 모르면 임의 변경하지 않는다.
- NEEDS_CONTEXT: DB 소유 라벨은 컨테이너 재생성 금지와 충돌하므로 자동 이전 미지원. WEB/WAS만 승인 이전 가능하다.
- 수정12의 smoke_groups/소요시간/판단 근거 필드와 병합할 때 스키마를 재생성한다. AGENTS 수정은 인계서에 제안만 남겼다.

## DB 승계 원장 선택 — 추가 사용자 결정

DB 컨테이너/볼륨/라벨을 유지하고 별도 관리 소유권 원장을 설계한다. 구현을 허용한 것으로 확대하지 않는다. 관측 해시 승인, 원장 revision CAS, 원 소유자의 향후 관리 차단, 다중 컨트롤러 제한을 `O1-db-ownership-succession-design-2026-10-03.md`에 구체화했다. 기존 NEEDS_CONTEXT는 설계 결정 완료로 대체하며, 실행 지원은 후속 구현이다.

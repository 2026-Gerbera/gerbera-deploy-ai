# 공통 계약 인덱스 (권위 문서)

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준)
>
> 역할 코드: O1 정준우 · O2 김준석 · O3 장민영 · C2 안승환 · C3 양서윤 · C1 = 유상준 하차(10/1), C1은 정준우·김준석 분담(분담 ✅, 세부 🟡 김준석 확인) · TL = 하네스·계약 승인자(미지정, 결정 필요).

**이 문서가 현재 하네스 계약의 진입점입니다. 최신 사용자 결정이 먼저입니다.** 우선순위: 이 인덱스와 여기서 링크한 계약 > `docs/guides/<역할>.md` > 코드. 문서와 코드가 충돌하면 멈추고 하네스·계약 승인자(@TL, 지정 대기)에게 묻습니다.

- 상태: **O1 착수 계약 반영, 팀 연동 계약 일부 초안.** [O1 결정](../decisions/2026-09-30-o1-start-contracts.md)이 승인·스냅샷·이미지 계약의 기준입니다. 나머지 `TODO(contract)`는 팀 연동 시 확정합니다. 설계 초안은 [02 파이프라인과 계획](../../../single-app/docs/02_파이프라인과-계획.md), [04 툴 카탈로그](../../../single-app/docs/04_툴-카탈로그.md)에 있습니다.
- v1 동결: 💭 팀이 정한 시각. 동결 뒤 변경은 [계약 변경 절차](../harness/03_깃-규칙과-AI-컨트리뷰터-차단.md)(E-2)를 따릅니다.
- 계약 파일: `src/ddak/core/contracts/**`, `src/ddak/core/registry.py`(카탈로그), `contracts/schemas/**`, `docs/contracts/**`. 변경 PR에는 `make contracts-update` 결과를 포함합니다.

| 계약 | 파일(모델 경로 · 스키마 경로) | 소유 | 상태 | 소비자 |
|---|---|---|---|---|
| 툴 카탈로그(40개: 모듈·단계·uses_ai·step 층·대상·영향·담당·플래그) | `ddak/core/registry.py` · `contracts/schemas/tool_catalog.json` | 하네스 소유자 + O1 + O2 | 초안(40개 반영, 💭 레지스트리 트리 9/30. 인프라 툴 5개·Dockerfile 툴 2개·`push_image`는 💭 이름) | 전원 |
| 툴별 입출력 모델 | `ddak/core/contracts/tools/<tool>.py` · `contracts/schemas/<tool>.*.json` | 툴 담당 + 하네스 소유자 | 초안(예시 ping만) | 실행기(O1), 관리 웹(C3) |
| plan.json(최종 계획) | `ddak/core/contracts/plan.py` · `contracts/schemas/plan.json` | O2 + O1 | 💭 초안(실행기 예시에 필요한 모양) | validate_plan(O2), 실행기(O1), 관리 웹(C3) |
| AI 초안(PlanDraft)·step 카탈로그·skip 규칙 | `ddak/plan/`(예정) | O2 | TODO(contract) | validate_plan(O2) |
| 배포 요청·deploy.yaml | `ddak/core/contracts/deploy_request.py`, `deploy_config.py` · `contracts/schemas/deploy_config.json` | O2 + O1 | TODO(contract) | 전원 |
| 어댑터 규약 | `ddak/core/adapters.py`(`TargetAdapter`, `AdapterSet`, `select_adapter`) | 하네스 소유자 + O1 + C2 | 초안(Protocol만) | plan·infra·ci·verify 담당 |
| CD 공통 인터페이스(💭 함수·인자) | `ddak/cd/interface.py`(`CdProvider`, `ProviderResult`, `INTERFACE_FUNCTIONS`) + `ddak/cd/dispatch.py`(`select_provider`) + `ddak/cd/fake.py`. provider 구현은 `ddak/cloud/deploy/providers/aws.py`(위임, PR #1 merge 뒤)·`ddak/onprem/deploy/provider.py` | 하네스 소유자 + O1 + C2 | 초안(구현 TODO) | cd 담당(aws C2·정준우(C1 `ensure_tls`)·C3, onprem O1·O3) |
| 이미지 저장소 어댑터 | `ddak/cloud/build/registries/`(기본 `dockerhub`, 옵션 `ecr`, digest 고정 참조) | C2 | 초안(참조 조립·검사만) | ci·cd(환경 정보의 이미지 참조) |
| 진행 이벤트 · 스모크 결과 | `ddak/core/contracts/events.py`(`RunEvent`) · `contracts/schemas/events.json` | O1 + C3 (스모크 O3) | 💭 초안 | 관리 웹(C3), compare_env_results(O3) |
| 실행 컨텍스트 | `ddak/core/contracts/context.py`(`RunContext`) · `var/runs/<run_id>/context.json` | O1 + 하네스 소유자 | 💭 초안 | 모든 툴(읽기만) |
| 환경 정보(💭 형식) | 원본 4곳(Terraform 출력 `terraform output -json` · 관리 페이지 프로젝트 설정 · 온프렘 인벤토리 · 실행기 DB)을 코드가 읽어 run마다 스냅샷 → `RunContext`. LLM에는 가린 요약만. 레거시 편입(⏸) 때는 `discover_existing` 탐지 결과로도 채울 수 있게 | O1(C1 몫 포함) + O2(인벤토리) + C3(설정) | TODO(contract) | 모든 툴(읽기만), generate_plan·generate_infra(가린 요약만) |
| AI 출력 스키마 5종(채팅 의도, 분석 분류, step 선택, 실패 원인 설명, 보고 요약) + AI Terraform 초안(HCL 파일 묶음, `generate_infra`) + Dockerfile 초안(`generate_dockerfile`) | 각 AI 툴의 출력 모델 | O2(+O3, C3), HCL은 김준석(O2, `generate_infra`, 🟡), Dockerfile은 💭 O3 | TODO(contract) | call_ai, 관리 웹, validate_infra, validate_dockerfile |
| 인프라 계약(💭): 인프라 요구(`InfraNeeds`, 분석 결과에서 도출), AI Terraform 초안 메타(입력 해시·파일 목록·코드 소유 틀 버전), 검증 결과(정적 게이트·validate·Checkov), plan 요약(동작별 개수, delete·replace 목록, IAM diff, Access Analyzer 결과, 민감값 가림, plan sha256), 승인 기록(승인자·시각·plan sha256), 인프라 출력(`platform.cloud.json`) | `ddak/core/contracts/tools/<infra 툴>.py`(예정) | C1 분담: 정준우(검증·plan 요약·승인 기록·출력) + 김준석(요구 도출·초안 메타)(+C3 승인 화면, C2 출력 소비) | TODO(contract) | validate_plan(O2), 관리 웹(C3), 실행기(O1), 클라우드 어댑터(C2) |
| O1 승인 기록 | `ddak/core/contracts/approval.py` · `contracts/schemas/approval.json` | O1(화면 C3 연동) | O1 우선 기준: 한 클릭, 대상별 기록 | O1(`apply_infra` 포함)·C3·O3 |
| 스냅샷·이미지 산출물 | `ddak/core/contracts/release.py` · `contracts/schemas/release_artifacts.json` | O1(C2/O3 연동) | O1 우선 기준: 원본/수정본, index/플랫폼/관측 분리 | O1·C2·O3·C3 |
| ErrorCode | `ddak/core/contracts/errors.py` | 하네스 소유자 | 초안(💭 15개) | 실행기(O1), 관리 웹(C3) |

## 계약 문서를 쓸 때

- 필드마다 이름, 타입, 필수 여부, 예시, 누가 채우고 누가 읽는지를 적습니다.
- 도메인·호스트·IP·이미지 digest·시크릿 위치는 plan.json과 툴 파라미터에 넣지 않고 실행 컨텍스트로만 흐르게 합니다(✅ 장부 6·17).
- 비밀값이 들어가는 필드는 `SecretStr`로 두고, 어디에도 저장되지 않는 경로를 적습니다. SECRET_KEY는 시스템 난수(✅ 장부 13).
- AI 툴 출력에는 `ai_usage`와 `source`(live/cache/fixture/replay)를 넣습니다. AI 출력은 enum 중심으로 값·주소·명령을 담을 수 없게 합니다.
- 동결 뒤 새 필드는 optional로만 추가합니다.

# CD 구조: 공통 인터페이스 + provider 모듈

- 날짜: 2026-09-30
- 상태: 결정(CD 구조 ✅). 레지스트리 트리 전체 배치는 제안(💭, [I-38](../harness/06_결정-필요-항목.md))
- 관련 항목: [I-38](../harness/06_결정-필요-항목.md), 설계 문서 00 장부 29, [04 툴 카탈로그](../../../single-app/docs/04_툴-카탈로그.md)
- 결정자: 사용자(정준우)·팀. 제안: 양서윤(C3), 보완: 김준석(O2)

## 맥락

- 옛 배치는 툴마다 `local.py`·`cloud.py` 어댑터를 두는 방식(`build_deploy` 모듈)이었다. 새 CSP(GCP·Azure)를 붙이려면 툴 수만큼 파일이 늘고, 한 환경의 배포 방법이 여러 툴 디렉토리에 흩어진다.
- 온프렘과 클라우드가 같은 단계(배포·롤백·헬스·마이그레이션·설정 주입·TLS)를 거치므로 공통 인터페이스로 묶을 수 있다.
- 3일 범위라 GCP·Azure 구현은 하지 않는다. CSP 우선순위는 AWS 1순위(✅).

## 선택지와 장단점

| 선택지 | 장점 | 단점 |
|---|---|---|
| A. 툴마다 환경별 어댑터(옛 방식) | 하네스에 이미 있음(`core/adapters.py`), 툴 단위로 병렬 개발 | CSP 추가 시 툴 수만큼 파일, 환경별 코드가 흩어짐 |
| B. 공통 인터페이스 + provider 모듈(양서윤 제안) | 환경 하나의 배포 방법이 파일 하나(`providers/aws.py`, `providers/onprem.py`)에 모임. CSP 추가 = provider 파일 추가. 온프렘 TLS처럼 "해당 없음"을 인터페이스가 표현 | provider 파일 하나에 여러 담당이 섞일 수 있음(aws: C2·C1·C3) → 커지면 함수별 모듈로 나눔 |
| B + 공통 설정값 Helm식 공유(김준석 제안) | 공통 값(deploy.yaml 공통 키) + provider별 덮어쓰기 + 환경 정보를 코드가 합쳐 같은 모양으로 넘김. 환경 차이가 "덮어쓰기 목록"으로 보임 | 템플릿 엔진을 쓰면 복잡해짐 → 값 병합만 하고 템플릿 엔진은 쓰지 않음(💭) |

## 결정

- **CD = 공통 인터페이스 + provider 모듈**(✅). 구현: `src/ddak/cd/providers/aws.py` + `providers/onprem.py`(+ 테스트용 `fake.py`). GCP·Azure는 후순위로 인터페이스만 두고 코드는 없다.
- 인터페이스 함수(💭 이름·인자): `deploy`, `rollback`, `health_check`, `migrate_db`, `inject_config`(설정·시크릿), `ensure_tls`(클라우드만, 온프렘은 `applicable=False` "해당 없음" 반환). 파일: `src/ddak/cd/interface.py`.
- 김준석 제안의 "공통 설정값 Helm식 공유"를 반영한다: deploy.yaml 공통 키 → provider별 덮어쓰기(`targets.local`/`targets.cloud`) → 환경 정보(코드가 채움) 순서로 병합해 provider에 넘긴다.
- 레지스트리 트리(💭 고려안, 팀 확인 필요): `plan/`(analyze, generate_plan, validate_plan, generate_dockerfile(+ validate_dockerfile)), `infra/`(discover_existing, generate_infra, validate_infra, plan_infra, apply_infra; providers/aws), `ci/`(build, push_image; registries/dockerhub 기본, ecr 옵션), `cd/`(interface + providers/aws, onprem), `verify/`(smoke_test, compare_env_results, verify_tls, diagnose, report). 기존 툴 이름은 유지한다(analyze = `analyze_project`, build = `build_image`, diagnose = `diagnose_parity_gap`, report = `post_report`).

## 이유

- 환경 차이 흡수가 발표의 중심이다. 환경별 구현이 provider 파일 하나에 모이면 "온프렘과 클라우드가 같은 인터페이스를 다르게 구현한다"를 코드로 보여 줄 수 있다.
- 툴 이름·입출력 계약(옛 33개)은 그대로라 실행기·계획 검증·plan.json은 바뀌지 않는다. provider 선택은 코드(target, 어댑터 모드)가 하고 AI는 고르지 않는다.

## 반대 의견

- (기록) 옛 방식(툴별 어댑터)은 하네스에 이미 있고 병렬 개발 단위가 작다는 장점이 있었다. provider 파일에 여러 담당이 섞이는 문제는 "커지면 함수별 모듈로 나누고 CODEOWNERS를 담당별로" 두는 것으로 대응한다.
- 트리 전체 배치(💭)는 팀 확인 전이다. 반대가 있으면 이 문서에 적는다.

## 되돌리는 조건

- provider 한 파일에서 머지 충돌이 반복되면 함수별 모듈로 나눈다(인터페이스는 유지).
- 3일 안에 GCP·Azure를 할 일은 없다. 요구가 생기면 provider 파일을 추가한다.

## 영향

- 하네스: `Module` enum(plan/infra/ci/cd/verify), 카탈로그 40개, import-linter 계약 7개 경로, CODEOWNERS, `.github/commit-scopes.txt`, `src/ddak/cd/interface.py`, `src/ddak/cd/providers/`, `tests/unit/test_cd_interface.py`, `contracts/schemas/tool_catalog.json`.

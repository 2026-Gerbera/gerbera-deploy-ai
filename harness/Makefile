# 명령 표준. 실제 구현은 scripts/dev.py 하나에 있다(단일 진입점). 여기에는 로직을 넣지 않는다.
# Windows: python scripts/dev.py <작업>
# 변수: ARGS(test 인자), ALL=1(type 전체), CLOUD=1(demo-reset), FULL=1(attribution 전체 이력)
# terraform apply/destroy 타깃은 만들지 않는다(사람이 직접 실행).

DEV := uv run python scripts/dev.py

.DEFAULT_GOAL := help
.PHONY: onprem-run onprem-plan unlock help setup setup-local fmt lint type test test-docker test-aws test-llm contracts contracts-update boundary attribution check ci run up run-fake contract-smoke demo-reset demo-local demo smoke-local smoke-cloud patch-eval preflight secrets-scan tf-plan clean

help: ## 타깃 목록
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## 클론 직후 1회: uv, 의존성, 훅, git 신원 확인
	python3 scripts/dev.py setup

setup-local: ## Git 없는 템플릿: 로컬 의존성만 준비
	python3 scripts/dev.py setup-local

fmt: ## ruff format + ruff check --fix
	$(DEV) fmt

lint: ## ruff format --check + ruff check
	$(DEV) lint

type: ## pyright (core 차단, ALL=1이면 나머지는 경고)
	$(DEV) type $(if $(ALL),--all,)

test: ## pytest (docker/aws/llm 마커 제외, ARGS로 경로 지정 가능)
	$(DEV) test $(ARGS)

test-docker: ## docker 마커 테스트(담당자 수동)
	$(DEV) test-docker

test-aws: ## aws 마커 테스트(자격증명·비용, 담당자 수동)
	$(DEV) test-aws

test-llm: ## llm 마커 테스트(비용, 담당자 수동)
	$(DEV) test-llm

contracts: ## 스키마·툴 카탈로그 스냅샷 드리프트 검사
	$(DEV) contracts

contracts-update: ## 스냅샷 갱신(계약 변경 PR에서만)
	$(DEV) contracts-update

boundary: ## import-linter + AI 경계 테스트
	$(DEV) boundary

attribution: ## origin/main..HEAD 작성자·트레일러 검사(FULL=1이면 전체 이력)
	$(DEV) attribution $(if $(FULL),--full,)

check: ## push·PR 전 필수(요약·소요 초 출력)
	$(DEV) check

ci: ## check - attribution (CI quality 잡)
	$(DEV) ci

run: ## 앱 1개(관리 웹 + 실행기 + 레지스트리), 127.0.0.1, real 어댑터
	$(DEV) run

up: run ## run과 같다(호스트 실행)

run-fake: ## 같은 기동, 모든 어댑터 Fake
	$(DEV) run-fake

onprem-run: ## 로컬 빌드 + Claude 관리 웹·감시 기동
	$(DEV) onprem-run

onprem-plan: ## 실행 중인 관리 웹에 현재 HEAD 계획 요청(PROJECT·REF 선택)
	$(DEV) onprem-plan $(if $(PROJECT),--project $(PROJECT),) $(if $(REF),--ref $(REF),)

unlock: ## 실행 중인 관리 웹에서 제품 잠금·차단 해제(PROJECT 선택)
	$(DEV) unlock $(if $(PROJECT),--project $(PROJECT),)

contract-smoke: ## 레지스트리 구현 현황(등록/미구현) + Fake 1회 호출
	$(DEV) contract-smoke

demo-reset: ## 데모 상태 초기화(CLOUD=1은 확인 입력)
	$(DEV) demo-reset $(if $(CLOUD),--cloud,)

demo-local: ## 로컬 트랙만 끝까지
	$(DEV) demo-local

demo: ## 로컬 ∥ 클라우드 전체 경로(리허설)
	$(DEV) demo

smoke-local: ## 로컬 스모크
	$(DEV) smoke-local

smoke-cloud: ## 클라우드 스모크
	$(DEV) smoke-cloud

patch-eval: ## AI 설정 패치 P0 평가(일반 기본 OFF, 데모 ON)
	$(DEV) patch-eval

preflight: ## 데모 전 사전 점검
	$(DEV) preflight

secrets-scan: ## gitleaks 전체 이력 스캔
	$(DEV) secrets-scan

tf-plan: ## terraform fmt/validate/plan (apply 타깃 없음)
	$(DEV) tf-plan

clean: ## 개발 캐시만 삭제(실행 기록·잠금 보존)
	$(DEV) clean

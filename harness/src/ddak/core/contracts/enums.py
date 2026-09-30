"""계약 전체에서 쓰는 열거형. 값 문자열은 JSON 스키마와 plan.json에 그대로 나간다.

기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준). MCP 서버 이름(ServerName)은 없어졌고
툴이 사는 곳은 Module로 표시한다. 9/30 결정(CD = 공통 인터페이스 + provider 모듈, ✅)에 맞춰
모듈을 레지스트리 트리 고려안(💭) plan / infra / ci / cd / verify + core + ops로 나눴다.
"""

from __future__ import annotations

from enum import StrEnum


class Target(StrEnum):
    """배포 대상 환경. 실행기가 plan.json의 트랙과 deploy.yaml로 정한다(AI가 고르지 않는다)."""

    LOCAL = "local"  # 온프레미스
    CLOUD = "cloud"  # AWS 서울


class Stage(StrEnum):
    """파이프라인 5단계 + 공통 + 운영. 단위·통합 테스트 단계는 없다."""

    PLAN = "plan"  # ① 플랜
    VALIDATE = "validate"  # ② 계획 검증
    BUILD = "build"  # ③ 빌드
    DEPLOY = "deploy"  # ④ 배포
    VERIFY_REPORT = "verify_report"  # ⑤ 검증 및 보고
    COMMON = "common"
    OPS = "ops"


class Module(StrEnum):
    """툴이 사는 모듈. 패키지 경로는 ddak.<값>. 모듈끼리는 서로 import하지 않는다.

    💭 레지스트리 트리 고려안(2026-09-30). 단계(Stage)와 모듈은 1:1이 아니다
    (예: health_check는 ⑤ 단계지만 CD 인터페이스 함수라 cd 모듈, apply_infra는 ④ 단계지만 infra).
    """

    PLAN = "plan"  # ① 플랜 + ② 계획 검증(분석, 계획, 계획 검증, Dockerfile 생성·검사)
    INFRA = "infra"  # AI Terraform: 탐지·생성·검사·plan·apply (providers/aws)
    CI = "ci"  # ③ 빌드·푸시 (registries/dockerhub 기본, ecr 옵션)
    CD = "cd"  # ④ 배포: 공통 인터페이스 + providers/aws, onprem (GCP·Azure는 인터페이스만)
    VERIFY = "verify"  # ⑤ 검증 및 보고(스모크, 교차 비교, TLS 검증, 원인 설명, 보고)
    CORE = "core"  # 공용: call_ai, request_approval, stream_progress (+ 예시 툴 ping)
    OPS = "ops"  # 운영: preflight_check, reset_demo_state, cleanup (계획에 들어가지 않음)


class ToolKind(StrEnum):
    """툴의 모양. 실행기가 plan.json으로 부를 수 있는 것은 TOOL_FN뿐이다."""

    TOOL_FN = "tool_fn"  # @tool 데코레이터로 등록하는 함수
    LIBRARY = "library"  # 파이썬 라이브러리 함수(call_ai)
    EXECUTOR_FN = "executor_fn"  # 실행기 내부 기능(request_approval, stream_progress)


class Layer(StrEnum):
    """step 층(✅ 장부 5). 툴의 기본값이고, step 카탈로그가 대상별로 덮어쓸 수 있다.

    예: ensure_tls는 cloud에서 필수. 계획 검증(validate_plan, 코드)이 층 규칙을 강제한다.
    """

    BUILTIN = "builtin"  # 실행기 내장: 계획에 안 나옴(잠금, 상태 기록, 롤백, 이벤트)
    MANDATORY = "mandatory"  # 필수: AI가 뺄 수 없음(헬스, 스모크, 클라우드 TLS 검증 등)
    CONDITIONAL = "conditional"  # 조건부: 결정적 규칙 조건이 참일 때만 뺄 수 있음
    OPTIONAL = "optional"  # 선택: AI 자유(안전에 영향 없는 것만)
    OUTSIDE = "outside"  # 계획 밖: 계획을 만드는 쪽, 관문, 운영 스크립트, 라이브러리


class Effect(StrEnum):
    """step이 대상 환경에 주는 영향(💭). 대기 지점 규칙(W1~W3)의 근거다.

    클라우드 트랙의 STATE_CHANGE step은 반드시 wait_for: local_verified 뒤에 둔다(✅ 장부 6).
    """

    READ = "read"  # 읽기만(조회, 확인, 스모크의 테스트 데이터 쓰기 포함)
    ADDITIVE_PREP = (
        "additive_prep"  # 서비스 동작을 바꾸지 않는 추가형 준비(새 시크릿, 새 리비전 등록)
    )
    STATE_CHANGE = "state_change"  # 서비스 동작·공유 데이터를 바꿈(마이그레이션, 서비스 전환)


class RunMode(StrEnum):
    """run 종류. 초기 배포도 같은 파이프라인으로 한다(✅ 장부 14)."""

    UPDATE = "update"  # 업데이트 배포(데모: v2)
    BOOTSTRAP = "bootstrap"  # 초기 배포(v1, 이전 릴리스 없음)


class By(StrEnum):
    """결정 주체. 계획 step·패치 등을 규칙이 정했는지 AI가 정했는지 표시한다."""

    RULE = "rule"
    AI = "ai"


class Source(StrEnum):
    """AI 결과의 출처. live가 아니면 관리 페이지가 라벨을 표시한다(목업은 목업이라고 밝힘)."""

    LIVE = "live"
    CACHE = "cache"
    FIXTURE = "fixture"
    REPLAY = "replay"  # call_ai replay backend(저장된 응답)


class LLMBackend(StrEnum):
    """call_ai backend(✅ 장부 7)."""

    CLI = "cli"  # 개발: 운영자 본인 로컬에 로그인해 둔 Claude CLI(구독). 외부 공개 금지
    API = "api"  # 데모: Anthropic API 키
    REPLAY = "replay"  # 테스트·비상: 저장된 응답(화면에 "저장된 응답" 표시)

"""저장된 사실의 표시 사전. 실행 판정과 이벤트 형식은 바꾸지 않는다."""

from __future__ import annotations

import re

ENV = {"local": "온프레미스", "cloud": "클라우드", "common": "공통"}
TEXT = {
    "acquire_deploy_lock": {
        "description": "같은 프로젝트의 변경 작업이 겹치지 않도록 잠급니다.",
        "failed": "배포 잠금 확보를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "이번 실행의 배포 잠금을 확보했습니다.",
        "name": "배포 잠금 확보",
        "running": "같은 프로젝트의 변경 작업이 겹치지 않도록 잠급니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "analyze": {
        "description": "하드코딩된 설정과 개발 서버 주소의 파일·줄 위치를 찾습니다.",
        "failed": "환경 의존 코드 탐지를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "탐지 위치와 설정 키를 정리했습니다. 값은 가립니다.",
        "name": "환경 의존 코드 탐지",
        "running": "하드코딩된 설정과 개발 서버 주소의 파일·줄 위치를 찾습니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "analyze_project": {
        "description": "하드코딩된 설정과 개발 서버 주소의 파일·줄 위치를 찾습니다.",
        "failed": "환경 의존 코드 탐지를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "탐지 위치와 설정 키를 정리했습니다. 값은 가립니다.",
        "name": "환경 의존 코드 탐지",
        "running": "하드코딩된 설정과 개발 서버 주소의 파일·줄 위치를 찾습니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "규칙 판정",
    },
    "answer_code_question": {
        "description": "소스를 읽어 코드 질문에 답합니다. 배포는 시작하지 않습니다.",
        "failed": "코드 질문 답변을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "코드 질문 답변을 준비했습니다.",
        "name": "코드 질문 답변",
        "running": "소스를 읽어 코드 질문에 답합니다. 배포는 시작하지 않습니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "apply_infra": {
        "description": "승인한 변경 계획에 따라 리소스를 만들거나 갱신합니다.",
        "failed": "클라우드 리소스 적용을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "승인한 인프라 변경을 적용했습니다.",
        "name": "클라우드 리소스 적용",
        "running": "승인한 변경 계획에 따라 리소스를 만들거나 갱신합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "build.was": {
        "description": "승인한 소스로 이미지를 만들고 배포할 digest를 고정합니다.",
        "failed": "배포 이미지 빌드를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "배포용 이미지 빌드를 마쳤습니다.",
        "name": "WAS 이미지 빌드",
        "running": "WAS 이미지를 기록된 빌드 환경에서 만드는 중입니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "build_image": {
        "description": "승인한 소스로 이미지를 만들고 배포할 digest를 고정합니다.",
        "failed": "배포 이미지 빌드를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "배포용 이미지 빌드를 마쳤습니다.",
        "name": "배포 이미지 빌드",
        "running": "승인한 소스로 이미지를 만들고 배포할 digest를 고정합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "call_ai": {
        "description": "저장된 분석 사실을 바탕으로 변경안을 준비합니다.",
        "failed": "변경안 검토를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "변경안 검토 응답을 받았습니다. 검사는 별도로 진행합니다.",
        "name": "변경안 검토",
        "running": "저장된 분석 사실을 바탕으로 변경안을 준비합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "check_patch": {
        "description": "수정 패치가 소스에 적용되고 허용된 설정만 바꾸는지 검사합니다.",
        "failed": "코드 수정 검사를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "코드 수정 검사를 통과했습니다.",
        "name": "코드 수정 검사",
        "running": "수정 패치가 소스에 적용되고 허용된 설정만 바꾸는지 검사합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 검사",
    },
    "cleanup": {
        "description": "승인한 대상의 리소스를 정리합니다.",
        "failed": "리소스 정리를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "승인한 리소스 정리를 마쳤습니다.",
        "name": "리소스 정리",
        "running": "승인한 대상의 리소스를 정리합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "collect_diagnostics": {
        "description": "실패한 단계의 상태와 진단 단서를 모읍니다.",
        "failed": "실패 단서 수집을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "실패 단서를 모았습니다. 기술 정보에서 확인할 수 있습니다.",
        "name": "실패 단서 수집",
        "running": "실패한 단계의 상태와 진단 단서를 모읍니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "compare_env_results": {
        "description": "두 환경이 모두 검증을 마치면 같은 시나리오의 결과를 비교합니다.",
        "failed": "두 환경 동작 비교를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "두 환경의 동작 비교를 마쳤습니다.",
        "name": "두 환경 동작 비교",
        "running": "두 환경이 모두 검증을 마치면 같은 시나리오의 결과를 비교합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 검사",
    },
    "deploy.was.cloud": {
        "description": "선택한 티어를 승인한 이미지로 교체하고 준비 상태를 기다립니다.",
        "failed": "컨테이너 교체를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "선택한 티어의 컨테이너 교체를 마쳤습니다. 다음 검증에서 응답을 확인합니다.",
        "name": "WAS 컨테이너 교체",
        "running": "선택한 티어를 승인한 이미지로 교체하고 준비 상태를 기다립니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "deploy.was.local": {
        "description": "선택한 티어를 승인한 이미지로 교체하고 준비 상태를 기다립니다.",
        "failed": "컨테이너 교체를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "선택한 티어의 컨테이너 교체를 마쳤습니다. 다음 검증에서 응답을 확인합니다.",
        "name": "WAS 컨테이너 교체",
        "running": "선택한 티어를 승인한 이미지로 교체하고 준비 상태를 기다립니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "deploy_tier": {
        "description": "선택한 티어를 승인한 이미지로 교체하고 준비 상태를 기다립니다.",
        "failed": "컨테이너 교체를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "선택한 티어의 컨테이너 교체를 마쳤습니다. 다음 검증에서 응답을 확인합니다.",
        "name": "컨테이너 교체",
        "running": "선택한 티어를 승인한 이미지로 교체하고 준비 상태를 기다립니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "detect": {
        "description": "마지막 성공 배포와 비교해 변경된 파일과 티어를 찾습니다.",
        "failed": "변경 범위 탐지를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "변경된 파일과 재빌드할 티어를 정리했습니다.",
        "name": "변경 범위 탐지",
        "running": "마지막 성공 배포와 비교해 변경된 파일과 티어를 찾습니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "detect_changed_tiers": {
        "description": "마지막 성공 배포와 비교해 변경된 파일과 티어를 찾습니다.",
        "failed": "변경 범위 탐지를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "변경된 파일과 재빌드할 티어를 정리했습니다.",
        "name": "변경 범위 탐지",
        "running": "마지막 성공 배포와 비교해 변경된 파일과 티어를 찾습니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "diagnose_parity_gap": {
        "description": "검증 기록을 근거로 환경 간 차이의 원인을 설명합니다.",
        "failed": "두 환경 차이 원인 분석을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "환경 차이의 원인 설명을 준비했습니다. 배포 판정은 그대로입니다.",
        "name": "두 환경 차이 원인 분석",
        "running": "검증 기록을 근거로 환경 간 차이의 원인을 설명합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "규칙 판정",
    },
    "discover_existing": {
        "description": "이미 있는 리소스를 확인해 새로 만들 범위를 정합니다.",
        "failed": "기존 클라우드 리소스 확인을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "기존 리소스 확인을 마쳤습니다.",
        "name": "기존 클라우드 리소스 확인",
        "running": "이미 있는 리소스를 확인해 새로 만들 범위를 정합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "ensure_tls": {
        "description": "도메인과 인증서 연결 상태를 확인합니다.",
        "failed": "HTTPS 연결 준비를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "HTTPS 연결 준비를 마쳤습니다.",
        "name": "HTTPS 연결 준비",
        "running": "도메인과 인증서 연결 상태를 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "generate_dockerfile": {
        "description": "Dockerfile이 없으면 빌드 설정 초안을 만듭니다.",
        "failed": "이미지 빌드 설정 제안을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "빌드 설정 초안을 준비했습니다. 검사와 승인 뒤 사용합니다.",
        "name": "이미지 빌드 설정 제안",
        "running": "Dockerfile이 없으면 빌드 설정 초안을 만듭니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "AI 제안",
    },
    "generate_infra": {
        "description": "필요한 클라우드 리소스와 권한의 설정 초안을 만듭니다.",
        "failed": "인프라 변경안 작성을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "인프라 초안을 준비했습니다. 아직 리소스를 변경하지 않았습니다.",
        "name": "인프라 변경안 작성",
        "running": "필요한 클라우드 리소스와 권한의 설정 초안을 만듭니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "AI 제안",
    },
    "generate_plan": {
        "description": "변경된 티어와 환경에 필요한 작업을 고르고 순서를 정합니다.",
        "failed": "실행 계획 작성을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "포함할 단계와 제외할 단계를 정했습니다.",
        "name": "실행 계획 작성",
        "running": "변경된 티어와 환경에 필요한 작업을 고르고 순서를 정합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "규칙 판정",
    },
    "gitleaks": {
        "description": "수정 내용에 비밀값이 남아 있는지 검사합니다.",
        "failed": "비밀값 노출 검사를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "비밀값 노출 검사를 통과했습니다.",
        "name": "비밀값 노출 검사",
        "running": "수정 내용에 비밀값이 남아 있는지 검사합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 검사",
    },
    "health_check": {
        "description": "배포된 서비스가 정상 응답하는지 확인합니다.",
        "failed": "서비스 응답 확인을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "서비스 응답 검사를 통과했습니다.",
        "name": "서비스 응답 확인",
        "running": "배포된 서비스가 정상 응답하는지 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 검사",
    },
    "inject_env_config": {
        "description": "이 환경에 맞는 설정을 배포 대상에 주입합니다. 값은 표시하지 않습니다.",
        "failed": "환경 설정 주입을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "환경 설정 주입을 마쳤습니다.",
        "name": "환경 설정 주입",
        "running": "이 환경에 맞는 설정을 배포 대상에 주입합니다. 값은 표시하지 않습니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "intake": {
        "description": "배포할 브랜치와 커밋을 고정합니다.",
        "failed": "배포 요청 접수를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "배포 소스와 요청을 기록했습니다.",
        "name": "배포 요청 접수",
        "running": "배포할 브랜치와 커밋을 고정합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "patch_config": {
        "description": "개발값을 환경변수 조회로 바꾸고 이전 승인 패치가 유지되는지 확인합니다.",
        "failed": "환경 설정 코드 수정 제안을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "설정 수정 후보와 검사 결과를 준비했습니다. 승인 뒤 적용합니다.",
        "name": "환경 설정 코드 수정 제안",
        "running": "개발값을 환경변수 조회로 바꾸고 이전 승인 패치가 유지되는지 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "AI 제안",
    },
    "patch_db_access": {
        "description": "환경별 DB 연결을 사용할 코드 변경을 준비합니다.",
        "failed": "DB 연결 코드 수정 제안을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "DB 연결 수정 후보를 준비했습니다. 아직 적용하지 않았습니다.",
        "name": "DB 연결 코드 수정 제안",
        "running": "환경별 DB 연결을 사용할 코드 변경을 준비합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "patch_storage": {
        "description": "환경별 저장소를 사용할 코드 변경을 준비합니다.",
        "failed": "저장소 코드 수정 제안을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "저장소 수정 후보를 준비했습니다. 아직 적용하지 않았습니다.",
        "name": "저장소 코드 수정 제안",
        "running": "환경별 저장소를 사용할 코드 변경을 준비합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "plan": {
        "description": "변경된 티어와 환경에 필요한 작업을 고르고 순서를 정합니다.",
        "failed": "실행 계획 작성을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "포함할 단계와 제외할 단계를 정했습니다.",
        "name": "실행 계획 작성",
        "running": "변경된 티어와 환경에 필요한 작업을 고르고 순서를 정합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "plan_infra": {
        "description": "추가·변경·삭제할 리소스와 권한 변경을 계산합니다.",
        "failed": "리소스 변경 계획 확인을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "리소스 변경 계획을 준비했습니다. 적용에는 승인이 필요합니다.",
        "name": "리소스 변경 계획 확인",
        "running": "추가·변경·삭제할 리소스와 권한 변경을 계산합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "post_report": {
        "description": "확정된 배포와 검증 결과를 읽을 수 있는 요약으로 정리합니다.",
        "failed": "결과 보고를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "배포 결과를 기록했습니다. 설명 요약은 별도로 갱신됩니다.",
        "name": "결과 보고",
        "running": "확정된 배포와 검증 결과를 읽을 수 있는 요약으로 정리합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "preflight_check": {
        "description": "환경 연결과 배포에 필요한 준비 상태를 확인합니다.",
        "failed": "배포 연결 점검을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "배포 연결 점검을 마쳤습니다.",
        "name": "배포 연결 점검",
        "running": "환경 연결과 배포에 필요한 준비 상태를 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "prepare": {
        "description": "실행 계획과 코드·인프라 검사 결과를 승인 자료로 모읍니다.",
        "failed": "승인 자료 확정을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "승인 자료를 확정했습니다.",
        "name": "승인 자료 확정",
        "running": "실행 계획과 코드·인프라 검사 결과를 승인 자료로 모읍니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "prepare_db": {
        "description": "계획에 있는 DB 초기화 또는 추가형 마이그레이션을 실행합니다.",
        "failed": "DB 초기화·마이그레이션을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "계획된 DB 준비를 마쳤습니다.",
        "name": "DB 초기화·마이그레이션",
        "running": "계획에 있는 DB 초기화 또는 추가형 마이그레이션을 실행합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "prepare_storage": {
        "description": "앱에서 사용할 저장소와 접근 설정을 준비합니다.",
        "failed": "저장소 준비를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "앱 저장소 준비를 마쳤습니다.",
        "name": "저장소 준비",
        "running": "앱에서 사용할 저장소와 접근 설정을 준비합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "push_image": {
        "description": "이미지 저장소에 배포할 이미지가 올라갔는지 확인합니다.",
        "failed": "이미지 저장 확인을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "저장소의 이미지 digest를 확인했습니다.",
        "name": "이미지 저장 확인",
        "running": "이미지 저장소에 배포할 이미지가 올라갔는지 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "receive_deploy_request": {
        "description": "배포할 브랜치와 커밋을 고정합니다.",
        "failed": "배포 요청 접수를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "배포 소스와 요청을 기록했습니다.",
        "name": "배포 요청 접수",
        "running": "배포할 브랜치와 커밋을 고정합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "record_deploy_log": {
        "description": "배포·검증·복구 기록을 저장합니다.",
        "failed": "실행 기록 저장을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "실행 기록을 저장했습니다.",
        "name": "실행 기록 저장",
        "running": "배포·검증·복구 기록을 저장합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "request_approval": {
        "description": "코드·배포·인프라 대상별 해시를 한 화면에서 확인합니다.",
        "failed": "변경 승인을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "변경 승인 기록을 저장했습니다.",
        "name": "변경 승인",
        "running": "코드·배포·인프라 대상별 해시를 한 화면에서 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "reset_demo_state": {
        "description": "승인한 시연 상태 초기화 작업을 실행합니다.",
        "failed": "시연 상태 초기화를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "시연 상태 초기화 작업을 마쳤습니다.",
        "name": "시연 상태 초기화",
        "running": "승인한 시연 상태 초기화 작업을 실행합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "rollback_tier": {
        "description": "실패한 환경을 이전 배포 상태로 되돌립니다.",
        "failed": "이전 버전 복구를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "이전 배포로 복구했습니다.",
        "name": "이전 버전 복구",
        "running": "실패한 환경을 이전 배포 상태로 되돌립니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "smoke_test": {
        "description": "배포된 앱에서 주요 사용자 동작을 확인합니다.",
        "failed": "사용자 시나리오 확인을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "사용자 시나리오 검사를 통과했습니다.",
        "name": "사용자 시나리오 확인",
        "running": "배포된 앱에서 주요 사용자 동작을 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 검사",
    },
    "source": {
        "description": "소스와 수정 후보를 검사하고 승인할 해시를 고정합니다.",
        "failed": "배포 소스 검사를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "배포 소스와 승인할 해시를 준비했습니다.",
        "name": "배포 소스 검사",
        "running": "소스와 수정 후보를 검사하고 승인할 해시를 고정합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "stream_progress": {
        "description": "실행 단계의 상태를 이 화면에 전달합니다.",
        "failed": "실행 기록 수신을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "실행 기록 수신을 마쳤습니다.",
        "name": "실행 기록 수신",
        "running": "실행 단계의 상태를 이 화면에 전달합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "sync_env_to_cloud": {
        "description": "필요한 비밀값을 클라우드 보관소에 준비합니다. 값은 표시하지 않습니다.",
        "failed": "클라우드 비밀값 준비를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "클라우드 비밀값 준비를 마쳤습니다.",
        "name": "클라우드 비밀값 준비",
        "running": "필요한 비밀값을 클라우드 보관소에 준비합니다. 값은 표시하지 않습니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "validate": {
        "description": "필수 단계·의존 순서·허용된 작업인지 검사합니다.",
        "failed": "계획 검증을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "실행 계획이 검사 기준을 통과했습니다.",
        "name": "계획 검증",
        "running": "필수 단계·의존 순서·허용된 작업인지 검사합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "validate_dockerfile": {
        "description": "빌드 설정의 보안 규칙과 실제 빌드 가능 여부를 검사합니다.",
        "failed": "빌드 설정 검사를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "빌드 설정 검사를 통과했습니다.",
        "name": "빌드 설정 검사",
        "running": "빌드 설정의 보안 규칙과 실제 빌드 가능 여부를 검사합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 검사",
    },
    "validate_infra": {
        "description": "설정 문법·정책·권한 경계가 기준을 지키는지 검사합니다.",
        "failed": "인프라 설정 검사를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "인프라 설정 검사를 통과했습니다.",
        "name": "인프라 설정 검사",
        "running": "설정 문법·정책·권한 경계가 기준을 지키는지 검사합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 검사",
    },
    "validate_plan": {
        "description": "필수 단계·의존 순서·허용된 작업인지 검사합니다.",
        "failed": "계획 검증을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "실행 계획이 검사 기준을 통과했습니다.",
        "name": "계획 검증",
        "running": "필수 단계·의존 순서·허용된 작업인지 검사합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 검사",
    },
    "verify.compare": {
        "description": "두 환경이 모두 검증을 마치면 같은 시나리오의 결과를 비교합니다.",
        "failed": "두 환경 동작 비교를 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "두 환경의 동작 비교를 마쳤습니다.",
        "name": "두 환경 동작 비교",
        "running": "두 환경이 모두 검증을 마치면 같은 시나리오의 결과를 비교합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "verify.smoke.cloud": {
        "description": "배포된 앱에서 주요 사용자 동작을 확인합니다.",
        "failed": "사용자 시나리오 확인을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "사용자 시나리오 검사를 통과했습니다.",
        "name": "사용자 시나리오 확인",
        "running": "배포된 앱에서 주요 사용자 동작을 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "verify.smoke.local": {
        "description": "배포된 앱에서 주요 사용자 동작을 확인합니다.",
        "failed": "사용자 시나리오 확인을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "사용자 시나리오 검사를 통과했습니다.",
        "name": "사용자 시나리오 확인",
        "running": "배포된 앱에서 주요 사용자 동작을 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
    "verify_tls": {
        "description": "외부 주소의 인증서와 HTTPS 응답을 확인합니다.",
        "failed": "HTTPS 확인을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "HTTPS 검사를 통과했습니다.",
        "name": "HTTPS 확인",
        "running": "외부 주소의 인증서와 HTTPS 응답을 확인합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 검사",
    },
    "watch_post_deploy": {
        "description": "배포 후 응답이 계속 안정적인지 관찰합니다.",
        "failed": "배포 후 상태 관찰을 완료하지 못했습니다. 원인과 다음 행동을 확인하세요.",
        "finished": "배포 후 상태 관찰을 마쳤습니다.",
        "name": "배포 후 상태 관찰",
        "running": "배포 후 응답이 계속 안정적인지 관찰합니다.",
        "skipped": "이번 실행에서는 건너뛰었습니다.",
        "waiting": "앞 단계가 끝나면 시작합니다.",
        "actor": "코드 실행",
    },
}
ALIASES = {
    "analyze": "analyze_project",
    "deploy.config": "inject_env_config",
    "deploy.dbinit": "prepare_db",
    "deploy.infra": "apply_infra",
    "deploy.migrate": "prepare_db",
    "deploy.secrets": "sync_env_to_cloud",
    "deploy.storage": "prepare_storage",
    "deploy.tls": "ensure_tls",
    "detect": "detect_changed_tiers",
    "intake": "receive_deploy_request",
    "migrate": "prepare_db",
    "plan": "generate_plan",
    "prepare": "prepare",
    "prepare.infra": "apply_infra",
    "rollback": "rollback_tier",
    "run_migration": "prepare_db",
    "run_migrations": "prepare_db",
    "source": "source",
    "validate": "validate_plan",
    "verify.compare": "compare_env_results",
    "verify.diagnose": "diagnose_parity_gap",
    "verify.health": "health_check",
    "verify.report": "post_report",
    "verify.smoke": "smoke_test",
    "verify.tls": "verify_tls",
    "verify.watch": "watch_post_deploy",
}
SKIP_REASONS = {
    "tree_unchanged": "소스가 같아 이전 이미지를 재사용합니다.",
    "digest_deployed": "같은 이미지가 이미 배포되어 있습니다.",
    "no_new_migrations": "새 DB 변경이 없습니다.",
    "no_migrations": "새 DB 변경이 없습니다.",
    "db_initialized": "DB가 이미 초기화돼 초기화 작업을 생략합니다.",
    "no_new_keys": "새 설정 키가 없습니다.",
    "no_new_secret": "새 비밀 키가 없습니다.",
    "no_infra_change": "인프라 입력이 같아 리소스 적용을 건너뜁니다.",
    "optional": "이번 실행에서 선택 작업을 제외했습니다.",
}
ERRORS = {
    "LOCK_HELD": "다른 배포가 진행 중입니다. 완료 뒤 다시 시도하세요.",
    "LOCK_INVALID": "배포 잠금이 바뀌었습니다. 운영 상태를 확인하세요.",
    "CONFIG_INVALID": "설정 입력과 연결을 확인하세요.",
    "PLAN_INVALID": "배포 계획이 검사를 통과하지 못했습니다.",
    "INFRA_MISSING": "필요한 클라우드 구성이 없습니다.",
    "ADAPTER_TIMEOUT": "대상 환경의 응답이 제한 시간을 넘겼습니다.",
    "ADAPTER_FAILED": "대상 환경에서 작업을 완료하지 못했습니다.",
    "PRECONDITION_FAILED": "실행 기준이 바뀌었습니다. 최신 화면을 확인하세요.",
    "APPROVAL_REQUIRED": "변경을 확인한 뒤 승인해야 합니다.",
    "APPROVAL_DENIED": "이 요청은 거절되었습니다.",
    "TOGGLE_OFF": "이 작업은 설정에서 꺼져 있습니다.",
    "AI_NOT_ALLOWED": "이 작업에서는 수정 제안을 만들 수 없습니다.",
    "AI_UNAVAILABLE": "수정 제안 서비스에 연결하지 못했습니다.",
    "AI_OUTPUT_INVALID": "제안이 자동 검사를 통과하지 못했습니다.",
    "INTERNAL": "처리를 완료하지 못했습니다. 기술 정보를 확인하세요.",
    "REQUEST_FAILED": "요청을 처리하지 못했습니다. 다시 시도하세요.",
}
SCENARIOS = {
    "S0.version": "배포 버전 응답",
    "S0.ready": "준비 상태",
    "B1": "글 목록 열림",
    "B2.create": "새 글 작성",
    "B2.empty": "빈 제목 거부",
    "B2.long": "긴 제목 거부",
    "V2.box": "v2 박스·이미지",
}
GATES = {
    "images_ready": "이미지 준비",
    "infra_ready": "클라우드 구성 적용",
    "local_verified": "온프레미스 검증 통과",
    "cloud_verified": "클라우드 검증 통과",
}
PATTERNS = {
    "secret_key": ("하드코딩 서명 키", "소스에 공유·유출 위험이 있는 서명 키가 있습니다."),
    "local_address": ("로컬 주소", "개발 서버 주소를 배포 환경에서 사용할 수 없습니다."),
    "cookie_secure": ("쿠키 보안", "서비스 주소에 맞는 쿠키 보안 설정이 필요합니다."),
    "proxy_fix": ("프록시 설정", "배포 환경의 프록시 전달 수에 맞춰야 합니다."),
}
RULES = {
    "R-ids": "지원하는 작업만 실행합니다.",
    "R-params": "허용한 입력만 사용합니다.",
    "R-mandatory": "필수 작업을 유지합니다.",
    "R-couple": "함께 필요한 작업을 포함합니다.",
    "R-gate": "앞 작업의 완료를 확인합니다.",
    "R-facts": "분석한 사실과 계획을 대조합니다.",
    "R-migration": "DB 변경을 교체 전에 실행합니다.",
    "unregistered_optional_tool": "AI 선택 제안은 이번 버전에서 지원하지 않아 규칙이 제외했습니다.",
    "ai_draft_item_dropped": "실행할 수 없는 선택 제안을 제외했습니다.",
    "migration_modified": "기존 DB 변경 파일이 수정되었습니다.",
    "unknown_smoke_group": "등록되지 않은 사용자 시나리오입니다.",
    "hardcoded_secret": "코드에 비밀값이 직접 적혀 있습니다.",
    "embedded_credential": "주소에 자격 증명이 포함되어 있습니다.",
    "private_key": "소스에 개인 키 파일이 있습니다.",
    "env_file": "소스에 환경 설정 파일이 포함되어 있습니다.",
}
CATEGORIES = {
    "secret_missing": "환경 값 누락",
    "db_schema": "DB 구조 불일치",
    "db_conn": "DB 연결",
    "db_tls": "DB 암호화 연결",
    "image_pull": "이미지 내려받기",
    "health_timeout": "서비스 준비 지연",
    "parity": "두 환경 동작 차이",
    "unknown": "원인 추가 확인",
}
SOURCES = {"replay": "저장 응답", "fixture": "검증용 데이터", "mock": "검증용 데이터"}
UNKNOWN = {
    "name": "실행 작업",
    "description": "기록된 작업을 실행합니다.",
    "running": "작업이 진행 중입니다.",
    "finished": "작업을 마쳤습니다.",
    "failed": "작업을 완료하지 못했습니다.",
    "waiting": "앞 단계가 끝나면 시작합니다.",
    "skipped": "이번 실행에서는 건너뛰었습니다.",
    "actor": "코드 실행",
}


def wording(
    step: str,
    tool: str | None = None,
    backend: str | None = None,
    storage: dict | None = None,
) -> dict:
    base = re.sub(r"\.(local|cloud)$", "", str(step or ""))
    key = (
        tool
        if tool in TEXT
        else ALIASES.get(
            base,
            "build_image"
            if base.startswith("build.")
            else "deploy_tier"
            if base.startswith("deploy.")
            else base,
        )
    )
    if key not in TEXT:
        key = (
            "build_image"
            if base.startswith("build.")
            else "deploy_tier"
            if base.startswith("deploy.")
            else key
        )
    out = dict(TEXT.get(key, UNKNOWN))
    if key in {"build_image", "deploy_tier"}:
        tier = base.split(".")[1].upper() if "." in base else "앱"
        out["name"] = f"{tier} 이미지 빌드" if key == "build_image" else f"{tier} 컨테이너 교체"
        if key == "build_image":
            place = {"local": "온프레미스 로컬 빌드", "codebuild": "클라우드 CodeBuild"}.get(
                backend, "기록된 빌드 환경"
            )
            out["running"] = f"{tier} 이미지를 {place}에서 만드는 중입니다."
    if step == "deploy.infra.cloud" and storage and storage.get("intent") in {"create", "remove"}:
        bucket = storage.get("bucket") or ""
        removing = storage["intent"] == "remove"
        action = "삭제" if removing else "생성"
        suffix = " (업로드 이미지 포함)" if removing else ""
        out.update(
            name=f"S3 이미지 저장소 삭제 · {bucket}{suffix}"
            if removing
            else f"S3 이미지 저장소 신규 생성 · {bucket}",
            description="승인한 S3 저장소와 업로드 이미지를 함께 삭제합니다."
            if removing
            else "로컬 img 디렉토리 저장 코드 탐지 → 클라우드 공유 저장소 필요",
            running=f"S3 버킷 {action} 중 · {bucket}{suffix}",
            finished=f"S3 버킷 {action}을 마쳤습니다 · {bucket}{suffix}",
            failed=f"S3 버킷 {action}을 완료하지 못했습니다 · {bucket}{suffix}",
            checklist="S3 저장소 삭제 계획 (업로드 이미지 포함)"
            if removing
            else "S3 저장소 생성 계획",
            warning=removing,
        )
    return out


def track_for(step: str, target: str | None = None) -> str:
    return (
        target
        if target in {"local", "cloud"}
        else "local"
        if step.endswith(".local")
        else "cloud"
        if step.endswith(".cloud")
        else "common"
    )


def planned_rows(plan: dict, backend: str | None = None, storage: dict | None = None) -> list[dict]:
    rows = []
    for section in [
        plan.get("build", {}),
        *plan.get("deploy", {}).values(),
        plan.get("verify", {}),
    ]:
        for skipped, entries in [
            (False, section.get("steps", [])),
            (True, section.get("skipped", [])),
        ]:
            for entry in entries:
                sid = entry.get("id", "")
                text = wording(sid, entry.get("tool"), backend, storage)
                why = SKIP_REASONS.get(
                    entry.get("skip_rule") or entry.get("rule") or entry.get("reason"),
                    text["skipped"],
                )
                rows.append(
                    {
                        "id": sid,
                        "tool": entry.get("tool", ""),
                        "track": track_for(sid, entry.get("target")),
                        **text,
                        "status": "skipped" if skipped else "waiting",
                        "sentence": why if skipped else text["waiting"],
                        "why": why if skipped else text["description"],
                        "started": None,
                        "elapsed_s": None,
                    }
                )
    return rows


def explain(code: str | None, kind: str = "error") -> str:
    dictionaries = {
        "error": ERRORS,
        "rule": RULES,
        "scenario": SCENARIOS,
        "source": SOURCES,
        "category": CATEGORIES,
        "gate": GATES,
    }
    return dictionaries.get(kind, {}).get(str(code or "").split(":", 1)[0]) or (
        "사용자 동작 확인"
        if kind == "scenario"
        else "확인할 준비 항목이 있습니다."
        if kind == "rule"
        else "작업을 완료하지 못했습니다. 기술 정보에서 원인을 확인하세요."
    )


def outcome_sentence(row: dict, record: dict) -> str:
    output = record.get("output") or {}
    if output.get("applicable") is False:
        return "이번 환경에는 해당하지 않는 작업입니다."
    if output.get("changed") is False:
        return "변경 없이 기존 상태를 유지했습니다."
    scenarios = output.get("scenarios")
    if isinstance(scenarios, list):
        passed = sum(s.get("ok") is True for s in scenarios if isinstance(s, dict))
        return f"사용자 시나리오 {len(scenarios)}개 중 {passed}개 통과."
    image = output.get("image_ref")
    if isinstance(image, str) and (digest := re.search(r"sha256:([a-f0-9]{64})", image)):
        return f"{row['finished']} 이미지 {digest[1][:12]}."
    return row["finished"]


def pipeline_view(
    run: dict,
    plan: dict,
    events: list[dict],
    backend: str | None = None,
    storage: dict | None = None,
) -> dict:
    rows = {row["id"]: row for row in planned_rows(plan, backend, storage)}
    for event in events:
        kind = event.get("type", "")
        sid = (
            event.get("preparation_stage")
            if kind == "stage.finished"
            else f"rollback.{event.get('target', 'common')}"
            if kind.startswith("rollback.")
            else event.get("step")
        )
        if not sid or kind not in {
            "stage.finished",
            "step.started",
            "step.finished",
            "step.skipped",
            "gate.waiting",
            "gate.failed",
            "rollback.started",
            "rollback.finished",
        }:
            continue
        row = rows.setdefault(
            sid,
            {
                "id": sid,
                "tool": event.get("tool", ""),
                "track": track_for(sid, event.get("target")),
                **wording(sid, event.get("tool"), backend, storage),
                "started": None,
                "elapsed_s": None,
            },
        )
        status = (
            "running"
            if kind.endswith(".started")
            else "waiting"
            if kind == "gate.waiting"
            else "failed"
            if kind == "gate.failed"
            else "skipped"
            if kind == "step.skipped"
            else event.get("status", "waiting")
        )
        row.update(status=status, elapsed_s=event.get("elapsed_s"))
        if kind.endswith(".started"):
            row["started"] = event.get("ts")
        row["sentence"] = (
            row["running"]
            if status == "running"
            else row["waiting"]
            if status == "waiting"
            else row["skipped"]
            if status == "skipped"
            else row["failed"]
            if status in {"failed", "check_failed"}
            else row["finished"]
        )
        if kind == "gate.failed":
            row["sentence"] = "앞 단계 실패로 대기를 해제했습니다."
        if sid == "verify.compare" and status == "skipped":
            row["sentence"] = "한 환경이 실패해 두 환경을 비교하지 않았습니다."
    for sid, record in (run.get("result") or {}).get("steps", {}).items():
        row = rows.setdefault(
            sid,
            {
                "id": sid,
                "tool": record.get("tool", ""),
                "track": track_for(sid),
                **wording(sid, record.get("tool"), backend, storage),
                "started": None,
                "elapsed_s": None,
            },
        )
        status = record.get("status", "waiting")
        row.update(status=status, elapsed_s=record.get("elapsed_s"))
        row["sentence"] = (
            row["running"]
            if status == "running"
            else row["waiting"]
            if status == "waiting"
            else outcome_sentence(row, record)
            if status in {"succeeded", "running", "waiting"}
            else row["failed"]
            if status in {"failed", "check_failed"}
            else row["skipped"]
        )
    for track in ENV:
        sequence = [row for row in rows.values() if row["track"] == track]
        last = max(
            (
                i
                for i, row in enumerate(sequence)
                if row.get("started") or row["status"] in {"succeeded", "failed", "check_failed"}
            ),
            default=-1,
        )
        for row in sequence[:last]:
            if row["status"] == "waiting":
                row.update(status="unrecorded", sentence="앞 작업의 실행 기록 없음")
    if run["status"] not in {"AWAITING_APPROVAL", "APPROVED", "RUNNING"}:
        for row in rows.values():
            if row["status"] in {"running", "waiting"}:
                row.update(
                    status="unrecorded",
                    sentence="실행 안 함 · 앞 단계 실패"
                    if run["status"].startswith("FAILED")
                    else "완료 기록 없음",
                )
    order = {
        name: i
        for i, name in enumerate(
            ("intake", "detect", "analyze", "plan", "validate", "source", "prepare")
        )
    }
    ordered = sorted(rows.values(), key=lambda row: order.get(row["id"], 7))
    return {
        "rows": ordered,
        "lanes": {track: [row for row in ordered if row["track"] == track] for track in ENV},
        "texts": {
            **TEXT,
            **{sid: wording(sid, row.get("tool"), backend, storage) for sid, row in rows.items()},
        },
        "aliases": ALIASES,
        "gates": GATES,
        "errors": ERRORS,
    }


def preparation_rows(
    events: list[dict], *, code_patch: bool = False, cloud: bool = False
) -> list[dict]:
    recorded = {
        event.get("preparation_stage"): event
        for event in events
        if event.get("type") == "stage.finished"
    }
    names = ["intake", "detect", "analyze", "plan", "validate", "source", "prepare"]
    current = next((name for name in names if name not in recorded), None)
    failed = any(event.get("status") in {"failed", "check_failed"} for event in recorded.values())
    rows = []
    for name in names:
        event = recorded.get(name, {})
        text = wording(name)
        status = event.get("status") or ("running" if name == current and not failed else "waiting")
        rows.append(
            {
                "id": name,
                "name": text["name"],
                "actor": text["actor"],
                "status": status,
                "sentence": text["finished"]
                if status == "succeeded"
                else text["failed"]
                if status in {"failed", "check_failed"}
                else text["running"]
                if status == "running"
                else text["waiting"],
                "elapsed_s": event.get("elapsed_s"),
            }
        )
    for enabled, name in [(code_patch, "코드 수정 제안"), (cloud, "클라우드 구성 검사")]:
        if enabled:
            rows.append(
                {
                    "id": name,
                    "name": name,
                    "actor": "AI 제안" if name.startswith("코드") else "코드 검사",
                    "status": "unrecorded",
                    "sentence": "세부 진행 기록 없음 · 승인 자료를 준비할 때 처리합니다.",
                    "elapsed_s": None,
                }
            )
    return rows


def short_summary(text: str | None) -> str:
    """보고 설명을 작업 이름과 짧은 항목으로 한정한다. 판정에는 사용하지 않는다."""
    text = str(text or "")
    if '{"' in text or "{'" in text:
        return "배포 기록을 확인했습니다."
    for code, name in sorted(SCENARIOS.items(), key=lambda item: -len(item[0])):
        text = text.replace(code, name)
    text = re.sub(
        r"(?:verify|deploy|build|prepare)\.[A-Za-z0-9_.-]+", lambda m: wording(m[0])["name"], text
    )
    text = re.sub(r"\b(?:SUCCEEDED|FAILED_[A-Z_]+|DONE|RUNNING|null|None|error_code)\b", "", text)
    text = re.sub(r"sha256:[a-f0-9]+|\b[a-f0-9]{12,64}\b", "", text)
    text = re.sub(r"\b[A-Z][A-Z_]{3,}\b", "", text)
    text = re.sub(r"\b[A-Za-z_]+\.[A-Za-z0-9_.]+\b", "확인 작업", text)
    text = re.sub(r"\s+", " ", text).strip(" ·:,-()")
    return text[:55].rstrip() + "…" if len(text) > 56 else text or "배포 기록을 확인했습니다."

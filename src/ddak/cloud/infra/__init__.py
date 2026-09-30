"""cloud/infra(옛 infra/): AI Terraform(✅ 장부 21~23). 담당 C1(유상준). 툴 이름·개수·배치는 💭.

툴 5개: discover_existing, generate_infra, validate_infra, plan_infra, apply_infra.
- AI는 generate_infra에서 HCL 초안(제안)만 만든다. 탐지·검사·plan·apply는 결정적 코드이고
  AI 관문과 generate_infra를 import하지 않는다(import-linter 계약 7).
- apply_infra는 사람 승인(plan 파일 sha256) 뒤에만 apply한다. 자동 apply 없음(💭).
- 인프라 변경은 초기 배포와 "인프라 요구가 바뀐" 개선 배포에만 있다(예: v2 새 시크릿). 결과는
  환경 정보로 기록되고 다음 run은 코드가 읽는다(✅ 9/30). Terraform state는 `terraform output -json`
  으로만 읽고 원문을 파싱하지 않으며 LLM에 넣지 않는다.
- discover_existing은 읽기 전용이다. 레거시 편입(⏸)의 연결 지점이다.
- providers/: CSP별 코드 소유 틀(AWS 1순위, GCP·Azure는 후순위, ✅ 9/30).
"""

"""infra providers: CSP별 코드 소유 틀(💭). AWS만 있다(✅ 9/30: CSP 우선순위 AWS 1순위).

GCP·Azure는 후순위다. 모듈을 만들지 않는다(인터페이스만 문서에 둔다).
여기 코드는 generate_infra(AI)와 검사기(validate_infra·plan_infra·apply_infra)가 함께 읽으므로
AI 관문과 generate_infra를 import하지 않는다(import-linter 계약 7).
"""

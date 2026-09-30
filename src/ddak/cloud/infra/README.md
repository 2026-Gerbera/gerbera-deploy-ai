# cloud/infra (담당: 유상준, 옛 src/ddak/infra)
- 할 일: AI Terraform 생성(generate_infra)·검증·plan·apply, 기존 리소스 탐지. 툴은 tools/<이름>/tool.py, 코드 소유 틀은 providers/
- 입출력 계약: `src/ddak/core/contracts`
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(tools/generate_infra만 허용, 나머지는 계약 7로 금지).

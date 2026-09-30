"""AWS 코드 소유 틀(💭, 담당 C1). AI HCL을 안전하게 돌리기 위한 고정 값이다. AI가 바꾸지 못한다.

💭 이 틀은 앱마다 사람이 쓰는 Terraform이 아니라 제품 코드이므로 "딸깍" 취지에 맞는다는 것이
우리 권고다(사용자 확인 필요, 설계 문서 01 §5-5). 들어갈 것(TODO(C1)):
- 버전 고정(`required_version`, AWS provider `~> 6.66`, `.terraform.lock.hcl`), provider(region,
  allowed_account_ids, default_tags), backend partial config, 이름 규칙, 권한 경계 정책,
  상태 버킷 템플릿, 리소스 타입 허용 목록(약 30개), 정적 게이트 규칙.
- Terraform state는 플랫폼 층 / 앱 층으로 나눈다(데모 plan을 짧게, ✅ 9/30). 앱 층은 플랫폼
  층의 출력을 코드가 `terraform output -json`으로 읽어 변수로 넘긴다(state 원문·remote_state를
  읽지 않음).
"""

from __future__ import annotations

NAME = "aws"
REGION = "ap-northeast-2"  # 서울만
STATE_LAYERS = ("platform", "app")  # 💭 플랫폼 층(VPC·ALB·ECS 클러스터·공유 RDS·CodeBuild) / 앱 층

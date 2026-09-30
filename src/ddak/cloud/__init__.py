"""cloud: 클라우드(AWS) 팀 디렉토리. 하위 디렉토리가 담당 경계다.

- infra/ 유상준(C1): AI Terraform 생성·검증·plan·apply, 기존 리소스 탐지(옛 ddak/infra)
- build/ 안승환(C2): CodeBuild 멀티 아키텍처 빌드, registries/(dockerhub 기본, ecr 옵션)(옛 ddak/ci)
- deploy/ 안승환(C2): ECS·시크릿·DB, AwsProvider(CD 인터페이스 구현) 노출
- tls/ 유상준(C1): HTTPS 연결(ensure_tls)
- health/ 양서윤(C3): 클라우드 헬스·TLS 검증(health_check cloud, verify_tls)
AI import는 infra의 generate_infra만 허용한다(import-linter 계약 1·2·7).
"""

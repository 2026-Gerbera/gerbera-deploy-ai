"""deploy.yaml 모델(DeployConfig).

tier 목록, 빌드 입력 경로, 대상별 설정(public_scheme, proxy_fix 등).
정의되면 scripts/export_schemas.py가 contracts/schemas/deploy_config.json으로 내보낸다.

도메인은 deploy.yaml에 적지 않는다(✅ 장부 17: 관리 페이지 입력 -> 실행 컨텍스트 cloud_domain).
초안 모양은 설계 문서 03 골든패스 시나리오 2-4(single-app/docs/03)를 본다.

TODO(contract): 필드는 docs/contracts/ 계약 문서가 정한다. 하네스는 자리만 둔다.
"""

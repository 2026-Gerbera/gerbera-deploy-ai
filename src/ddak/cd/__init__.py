"""cd: ④ 배포. 공통 인터페이스 + provider 모듈(✅ 9/30, 양서윤 제안 채택 + 김준석 제안의
"공통 설정값 Helm식 공유" 반영). AI 없음(import-linter 계약 1).

툴 9개: acquire_deploy_lock, inject_env_config, sync_env_to_cloud, prepare_db, prepare_storage,
ensure_tls, deploy_tier, rollback_tier, health_check.
- interface.py: provider가 구현할 함수 6개(deploy, rollback, health_check, migrate_db,
  inject_config(설정·시크릿), ensure_tls(클라우드만, 온프렘은 "해당 없음" 반환)).
- provider 구현은 환경별 팀 디렉토리에 있다: ddak.cloud.deploy(AwsProvider, C2 안승환;
  ensure_tls는 ddak.cloud.tls C1 유상준, health_check는 ddak.cloud.health C3 양서윤) ·
  ddak.onprem.deploy(OnPremProvider, O1 정준우) · cd/fake.py(테스트).
  GCP·Azure는 후순위라 인터페이스만 있고 코드는 없다.
- dispatch.py: 툴(tool.py)은 target과 어댑터 모드로 provider를 고른다(select_provider).
  AI가 고르지 않는다. tools/는 환경 무관 툴(deploy_tier, inject_env_config, prepare_db,
  rollback_tier 등)이다.
- 공통 설정값(💭 Helm values처럼): deploy.yaml 공통 키 + provider별 덮어쓰기 + 환경 정보
  (코드가 채움)를 코드가 합쳐 provider에 같은 모양으로 넘긴다. 템플릿 엔진은 쓰지 않는다.
- 이미지 digest·도메인·시크릿 위치·서버 IP는 RunContext(환경 정보)에서 읽는다(파라미터로 받지 않음).
"""

"""cloud/deploy: CSP별 컨테이너 배포 provider. 담당 안승환(C2).

CD 인터페이스 구현을 노출한다. ddak.cd.dispatch가 이 공개 API만 import한다.
- providers/: aws.py, gcp.py, azure.py와 이름 기반 선택 함수
- provider.py: 기존 AwsProvider import 경로 호환용
- entry.py: ctx 진입점 deploy_service·rollback_service·put_secret_values·run_migrations
- ecs.py: 태스크 정의 새 리비전 + update_service, 실행 digest 관측(deploy·rollback)
- secrets.py: 키별 빈 시크릿에 PutSecretValue(inject_config)
- database.py: ECS 일회성 태스크로 마이그레이션(migrate_db)
- _platform.py: ctx.platform['cloud'] 키 이름(💭 일부 가정), _aws.py: 세션·오류 감싸기
AI를 import하지 않는다(import-linter 계약 1). 입출력은 ddak.cd.interface.ProviderResult.
"""

from __future__ import annotations

from ddak.cloud.deploy.entry import (
    deploy_service,
    put_secret_values,
    rollback_service,
    run_migrations,
)
from ddak.cloud.deploy.providers import AwsProvider, AzureProvider, GcpProvider, cloud_provider

__all__ = [
    "AwsProvider",
    "AzureProvider",
    "GcpProvider",
    "cloud_provider",
    "deploy_service",
    "put_secret_values",
    "rollback_service",
    "run_migrations",
]

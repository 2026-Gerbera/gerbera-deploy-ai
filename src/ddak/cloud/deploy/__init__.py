"""cloud/deploy: AWS 배포(ECS·Secrets Manager·DB 마이그레이션). 담당 안승환(C2).

CD 인터페이스 구현(AwsProvider)을 노출한다. ddak.cd.dispatch가 이 이름만 import한다.
- provider.py: AwsProvider(옛 cd/providers/aws.py). 인터페이스 함수 6개의 입구
- ecs.py: 태스크 정의 새 리비전 + update_service(deploy·rollback) — 빈 구현
- secrets.py: 빈 시크릿에 PutSecretValue(inject_config) — 빈 구현
- database.py: ECS 일회성 태스크로 마이그레이션(migrate_db) — 빈 구현
AI를 import하지 않는다(import-linter 계약 1). 입출력은 ddak.cd.interface.ProviderResult.
"""

from __future__ import annotations

from ddak.cloud.deploy.database import run_migrations
from ddak.cloud.deploy.ecs import deploy_service, rollback_service
from ddak.cloud.deploy.provider import AwsProvider
from ddak.cloud.deploy.secrets import put_secret_values

__all__ = [
    "AwsProvider",
    "deploy_service",
    "put_secret_values",
    "rollback_service",
    "run_migrations",
]

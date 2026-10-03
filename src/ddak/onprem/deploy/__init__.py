"""onprem/deploy: 온프렘 Docker 배포. 담당 정준우(O1).

CD 인터페이스 구현(OnPremProvider)을 노출한다. ddak.cd.dispatch가 이 이름만 import한다.
- provider.py: OnPremProvider(옛 cd/providers/onprem.py). 배포·롤백·설정 주입·마이그레이션
- containers.py: Docker CLI 경계 DockerHost(로컬 소켓 또는 SSH endpoint)
- ssh.py: VM 인벤토리의 호스트 지문 고정·접속 설정 격리
- replicas.py / health.py: 순차 교체·복구와 관측 기반 헬스 검사
- preflight.py / demo.py: 사전 점검과 보호된 데모 초기화
- config.py: 권한 제한 env 파일 검증·설정 주입
- migrate.py: C-09 마이그레이션 실행·결과 검증
AI를 import하지 않는다(import-linter 계약 1).
"""

from __future__ import annotations

from ddak.onprem.deploy.containers import DockerHost
from ddak.onprem.deploy.demo import reset_demo
from ddak.onprem.deploy.preflight import preflight_inventory
from ddak.onprem.deploy.preparation import (
    ApprovalCheck,
    ContainerObservation,
    ContainerObserver,
    ContainerTransfer,
    DatabaseAccount,
    DatabaseAction,
    DatabaseObservation,
    DatabasePreparationPlan,
    DatabasePreparationResult,
    DatabasePreparationSession,
    OwnerTransferPlan,
    TableObservation,
    apply_db_preparation,
    apply_owner_transfer,
    plan_db_preparation,
    plan_owner_transfer,
)
from ddak.onprem.deploy.provider import OnPremProvider
from ddak.onprem.deploy.provider import _Inventory as InventoryConfig
from ddak.onprem.deploy.provider import _Tier as TierConfig
from ddak.onprem.deploy.registration import (
    read_inventory,
    register_onprem_inventory,
    write_inventory,
)
from ddak.onprem.deploy.setup import OnPremPreparationManager, StdinRunner

__all__ = [
    "ApprovalCheck",
    "ContainerObservation",
    "ContainerObserver",
    "ContainerTransfer",
    "DatabaseAccount",
    "DatabaseAction",
    "DatabaseObservation",
    "DatabasePreparationPlan",
    "DatabasePreparationResult",
    "DatabasePreparationSession",
    "DockerHost",
    "InventoryConfig",
    "OnPremPreparationManager",
    "OnPremProvider",
    "OwnerTransferPlan",
    "StdinRunner",
    "TableObservation",
    "TierConfig",
    "apply_db_preparation",
    "apply_owner_transfer",
    "plan_db_preparation",
    "plan_owner_transfer",
    "preflight_inventory",
    "read_inventory",
    "register_onprem_inventory",
    "reset_demo",
    "write_inventory",
]

"""onprem/deploy: 온프렘 Docker 배포. 담당 정준우(O1).

CD 인터페이스 구현(OnPremProvider)을 노출한다. ddak.cd.dispatch가 이 이름만 import한다.
- provider.py: OnPremProvider(옛 cd/providers/onprem.py). 배포·롤백·설정 주입·마이그레이션
- containers.py: 로컬 Docker CLI 경계 DockerHost(옛 cd/docker_host.py)
- config.py·migrate.py: 설정 주입·마이그레이션을 provider.py에서 나눌 자리(지금은 비어 있음)
AI를 import하지 않는다(import-linter 계약 1).
"""

from __future__ import annotations

from ddak.onprem.deploy.containers import DockerHost
from ddak.onprem.deploy.provider import OnPremProvider

__all__ = ["DockerHost", "OnPremProvider"]

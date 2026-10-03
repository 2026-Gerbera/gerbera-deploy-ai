"""cloud/build(옛 ci/): ③ 빌드. 담당 C2(안승환), local(온프렘 pre-pull, 결정 필요)은 O1(정준우).
AI 없음(import-linter 계약 1).

툴: build_image. push_image는 구현·등록하지 않는다(10/2 C2 결정, 아래).
- CodeBuild가 모든 서비스를 이미지로 빌드한다(✅ 장부 9). 플랫폼 소유 인라인 buildspec.
- 이미지 저장소 기본 = Docker Hub(✅ 9/30). ECR은 옵션 어댑터(registries/ecr.py).
  CodeBuild는 Secrets Manager의 Docker Hub 읽기·쓰기 토큰으로 로그인해 push한다(빌드 역할은 그
  시크릿 하나만 읽음). 배포는 항상 digest 고정 `docker.io/<조직>/<이미지>@sha256:...`.
- push_image: CodeBuild 빌드 안에서 push하고 digest를 확인·내보내므로 build_image가 맡는다.
  카탈로그 이름은 남아 있지만 계획(step_catalog)과 계획 검증이 부르지 않아 등록하지 않는다.
- build_image 본체는 image.build_image → release.build_tier
  (step 하나 = tier 하나 = CodeBuild 한 번).
  툴 등록은 tools/build_image/tool.py.
- AWS 호출은 툴 호출마다 사용자 선택 프로필의 키를 고정하고 STS 기대 계정을 확인한다.
  프로필·기대 계정은 run 설정 우선이며 누락 시 제품 기본값을 쓴다.
"""

from ddak.cloud.build.image import build_image
from ddak.cloud.build.local import LocalBuildRunner, configure_local_build, preflight_local_build

__all__ = ["LocalBuildRunner", "build_image", "configure_local_build", "preflight_local_build"]

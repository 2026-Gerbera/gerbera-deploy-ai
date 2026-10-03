"""분석·계획 검증·스모크가 공유하는 배포 기능 표식과 허용 그룹. AI 없음."""

from ddak.core.storage import STORAGE_SMOKE_GROUP

V2_BOX_MARK = 'class="release-box"'
SMOKE_GROUPS = ("base", "v2", STORAGE_SMOKE_GROUP)

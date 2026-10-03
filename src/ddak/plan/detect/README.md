# plan/detect (담당: 정준우/O1, 2026-10-03 O2 승계)
- 환경별 마지막 성공 배포의 `source_files` manifest와 현재 소스를 비교해 바뀐 tier, 새 마이그레이션, facts 해시를 돌려준다(AI 없음, git 불필요).
- 공개: `detect_changed_tiers(inp, ctx)`(툴 `detect_changed_tiers`), `facts_reader(source)`(`DeploymentService.prepare`용).
- 입출력: `ddak.core.contracts.tools.detect_changed_tiers`. `previous`는 호출자(flow)가 Store에서 읽어 넘긴다(detect는 Store를 열지 않는다).
  Store 위치: `environments(project)[env]["current"]`의 run_id -> `releases.manifest`의 `source_files`. 없으면 None.
- 소스는 `DDAK_SOURCES_DIR`(기본 `var/sources`) / `source_dir`.

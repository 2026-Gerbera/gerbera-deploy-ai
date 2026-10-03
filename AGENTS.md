# 작업 위치

현재 하네스는 `harness/`에 있다. 코드 작업 전에 [harness/AGENTS.md](harness/AGENTS.md)를 읽는다. 명령은 `make -C harness <타깃>` 또는 `cd harness` 후 실행한다. `.git`과 `.github/workflows/`는 저장소 루트에 유지한다. 설계·조사 자료는 사용자 요청 없이 삭제하지 않는다.

# 대회 배포 기준

10/3 14:35 정준우 결정: 대회(10/4)는 클라우드·온프렘 모두 rolling이다. 블루그린 추가 작업은 대회 뒤로 미룬다. 기존 블루그린 코드는 휴면 분기로 보존한다. [결정 이력](harness/docs/decisions/2026-10-03-cloud-bluegreen.md)을 따른다.

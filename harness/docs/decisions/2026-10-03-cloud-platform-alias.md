# 2026-10-03 클라우드 플랫폼 이름(cloud_platform)

정준우 승인. 이유: 온프렘 프로젝트 이름(`flaskr-three`, 컨테이너 라벨 `ddak.project`)은 유지하고, 같은 AWS 계정에 이미 만든 클라우드 플랫폼(`flaskr`)을 재사용한다.

- 프로젝트 설정 키 `cloud_platform` 추가. 기본값은 프로젝트 이름(기존 동작). 소문자·숫자·하이픈, 40자 이하(클라우드 쪽 프로젝트 이름 규칙과 같다).
- 우선순위: 관리 페이지 저장값 > 기본 파일 `harness/config/defaults.toml`의 `[cloud_platform]` 표(프로젝트별, 지금 `flaskr-three = "flaskr"`) > 프로젝트 이름. 전역 `[project]` 표에는 두지 않는다.
- 클라우드 state bucket·state key·`var.project`·태그·경계·`generate_infra` 입력·기준본 경로·`image_repository` 출력·RDS DB 이름은 이 값을 쓴다. 승인 기록·실행 기록·잠금·스냅샷·온프렘은 계속 프로젝트 이름을 쓴다.
- 값은 run 시작 때 `project_settings`에 고정된다. 바꾸면 새 run의 인프라 plan(backend·변수)과 승인 해시가 달라져 다시 승인해야 한다.
- `DDAK_DOCKERHUB_NAMESPACE`가 없으면 프로젝트 이미지 저장소 설정(`image_repository`)의 앞부분을 쓴다. 둘 다 없을 때만 멈춘다.
- 플랫폼 적용 뒤 Docker Hub 시크릿(`ddak-platform/dockerhub-push·pull`) 채우기는 현재 값(AWSCURRENT)이 있는 시크릿을 덮어쓰지 않고 넘어간다. 값이 없는 시크릿이 있을 때만 `DDAK_DOCKERHUB_USER/PUSH_TOKEN/PULL_TOKEN`이 필요하다.

# compose/local — 로컬(온프레미스) 배포 대상 템플릿

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준)

- 담당: 김준석(O2, 온프렘 인프라 프로비저닝), 런타임 배포는 정준우(O1, `deploy_tier` → CD onprem provider).
- 구성: 샘플 앱 3티어(web nginx, was gunicorn, db MySQL 8.4). **DB는 두 환경 모두 MySQL**(✅ 장부 12). 클라우드는 공유 RDS MySQL(AI Terraform이 생성, 그 안에 앱 DB·계정, ✅ 장부 22).
- `compose.yaml`은 **db 서버와 네트워크만** 사전 구축합니다. web·was는 실행기가 Docker Hub(기본, ✅ 9/30)의 같은 digest를 **읽기 전용 토큰**으로 받아 올립니다(AWS 자격 불필요, ✅ 장부 9). 각 tier 호스트의 `docker login`은 김준석이 프로비저닝 때 합니다(토큰 값은 이 디렉토리에 두지 않음).
- 설계 원형은 tier별 서버(VM, `DOCKER_HOST=ssh://`)입니다. 자원이 부족하면 이 컨테이너 모드로 갑니다(✅ 장부 10). 발표 문구: "지금은 자원 이슈로 컨테이너로 진행했지만 실제는 tier별 서버가 존재하고 각각 배포하는 환경을 가정했다. 추후 고민할 방향."
- 온프렘 인벤토리(💭 `platform.onprem.yaml`: tier별 docker 주소, db 서버 IP, was upstream, `.env` 경로)가 서버 IP의 출처입니다. 실행기가 실행 컨텍스트에 넣습니다. AI가 IP를 만들지 않습니다.
- **로컬 HTTPS는 ⏸ 보류입니다.** 로컬은 `http://localhost:8080`으로 서빙합니다. 자체 서명 인증서를 쓰게 되면 브라우저 경고가 뜹니다.
- 비밀값은 compose 파일에 적지 않습니다. MySQL 비밀번호는 `compose/local/.secrets/`(gitignore)의 파일로, 앱 `SECRET_KEY`는 배포 때 시스템 난수로 만들어 was `.env`/컨테이너 env로 넣습니다(✅ 장부 13). env는 `docker inspect`로 보인다는 한계가 있습니다.
- 온프렘 DB 전송: PyMySQL 1.2+는 옵션이 없으면 **검증 없는 TLS**를 시도합니다. `ssl_disabled=True`로 평문을 명시할지는 결정 필요(교차 비교의 예상된 차이로 등록).
- 이미지는 💭 digest로 고정합니다. 데모 전에 `make preflight`로 미리 받아 둡니다.

```
cd compose/local
mkdir -p .secrets   # 비밀번호 파일 2개를 만든다(compose.yaml 머리 주석)
docker compose up -d db
docker compose ps    # db가 healthy인지 확인
```

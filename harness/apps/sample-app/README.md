# apps/sample-app — 샘플 앱(배포 대상)

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준)

- 담당: 장민영(O3, 짝 김준석 O2). 디렉토리 이름 `sample-app`은 💭 가칭입니다(결정 대기). 바꾸면 CODEOWNERS, `pyproject.toml`의 ruff 제외, `scripts/git_guard.py`의 `RUFF_EXCLUDED`, `.gitignore` 주석을 한 PR에서 함께 고칩니다.
- ✅ 베이스 앱은 Flask 공식 튜토리얼 **flaskr**입니다(pallets/flask `examples/tutorial`, BSD-3-Clause). 상세 시나리오: [03 골든패스 시나리오](../../../single-app/docs/03_골든패스-시나리오.md).
  - v1(사전 준비, 파이프라인으로 초기 배포): 로그인을 뺀 익명 게시판. **SQLAlchemy 기반**, MySQL 호환, 테이블명 `users` 권장.
  - v2(라이브 데모): 로그인 추가. WAS가 로그인 화면 템플릿을 렌더링하고 nginx 설정은 바꾸지 않습니다. 엔드포인트·로직 + `users` 테이블 추가형 마이그레이션, `post.author_id` NULL 허용, 목록은 LEFT JOIN.
- **DB는 MySQL 8.4**(✅ 장부 12). 앱은 DB 종류에 묶이지 않게 SQLAlchemy + `DATABASE_URL`로 연결합니다. 개발 PC는 SQLite여도 됩니다(MySQL DDL 파일은 SQLite에서 돌지 않으므로 개발 스키마 생성 방식은 결정 필요).
- RDS 연결 TLS: `require_secure_transport=ON`(RDS 기본 OFF). 클라이언트는 CA를 **검증**해야 합니다. PyMySQL은 `ssl={"ca": ...}` 딕셔너리 또는 SQLAlchemy URL `?ssl_ca=`를 씁니다. `ssl_ca=` 키워드만 주면 인증서를 검증하지 않습니다. `PyMySQL>=1.2.1` + `cryptography` 필수.
- **일부러 둔 개발값**(✅ 장부 13): 개발 환경 파일에 `DATABASE_URL=sqlite:///...`, `APP_BASE_URL=http://localhost:5000`, `SECRET_KEY=dev`. 배포 때 파이프라인이 대상 값으로 바꿉니다(온프렘 DB 서버 IP의 MySQL / RDS 엔드포인트 + TLS, 관리 페이지에 입력한 도메인, 시스템 난수 SECRET_KEY). 이 값들을 **고치지 않습니다.** AI 코딩 도구에도 같은 규칙을 적용합니다(AGENTS.md).
- 쿠키 Secure·ProxyFix는 **클라우드만** 켭니다(환경 플래그). `/version`에 연결된 DB 종류·호스트(가림)를 넣어 실제 연결을 확인합니다.
- flaskr 원본 `schema.sql`은 DROP으로 시작합니다. `init-db` 명령은 삭제하고 추가형 마이그레이션만 씁니다.
- **라이선스 고지:** flaskr 코드를 복사하면 원본 LICENSE(BSD-3-Clause) 전문과 저작권 고지를 이 디렉토리와 `THIRD_PARTY_NOTICES.md`에 남깁니다. 출처는 커밋 SHA까지 적습니다.
- **uv 프로젝트(src/ddak)와 ruff, CI 검사에서 제외합니다.** 외부 코드 원형과 데모용 개발값을 그대로 두기 위해서입니다. 자체 의존성 파일과 Dockerfile(web·was)을 이 디렉토리 안에 둡니다. 베이스 이미지는 💭 digest로 고정합니다.
- 데모는 이 원본이 아니라 `var/demo-workspace` 사본으로 돌립니다(`make demo-reset`이 다시 만듭니다). AI 패치(토글 ON, 기본 OFF)가 만드는 커밋은 사본 안에서만 만들고 팀 저장소에 push하지 않습니다.

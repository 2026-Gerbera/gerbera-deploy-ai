# apps/sample-app — 샘플 앱(배포 대상)

> 기준: 10/2 팀 결정([2026-10-02 팀 현황과 결정](../../docs/decisions/2026-10-02-team-status-and-decisions.md) 1·2·5·8번)

- 담당: 장민영(O3, 짝 김준석 O2). 디렉토리 이름 `sample-app`은 💭 가칭입니다(결정 대기). 바꾸면 CODEOWNERS, `pyproject.toml`의 ruff 제외, `scripts/git_guard.py`의 `RUFF_EXCLUDED`, `.gitignore` 주석을 한 PR에서 함께 고칩니다.
- **앱 코드는 별도 저장소에 있습니다: [2026-Gerbera/gerbera-application](https://github.com/2026-Gerbera/gerbera-application).** 브랜치·태그·환경 키·엔드포인트·v2 기능 추가 절차는 그 저장소 README가 기준입니다. 이 디렉토리에는 앱 설명과 실행 결과 기록(`results/`)만 둡니다.
- ✅ 베이스 앱은 Flask 공식 튜토리얼 **flaskr**입니다(pallets/flask `examples/tutorial`, BSD-3-Clause). 라이선스 전문과 출처 커밋은 앱 저장소의 `LICENSE.txt`·`THIRD_PARTY_NOTICES.md`에 있습니다.

## 브랜치와 데모 기준

| 이름 | 뜻 |
|---|---|
| `prod` (기본 브랜치) | 기준 코드. PR merge로 반영되면 파이프라인이 시작합니다(감시는 O2 `watch.py`) |
| `ai-prod` | 파이프라인이 `prod` merge + 승인된 AI 패치를 고정한 커밋. 빌드·배포는 이 커밋만 씁니다. 사람은 커밋하지 않습니다 |
| `main` | 실제로 배포된 코드의 기록. 배포를 일으키지 않습니다 |
| 태그 `v1` | 로그인 없는 익명 게시판(SQLAlchemy, MySQL 8.4, 추가형 마이그레이션 `0001`). migrate 결함 수정과 RDS CA 번들 포함 |
| 태그 `v2` | 1차 데모: v1 + 목록 페이지 이미지(인라인 SVG)·박스. 템플릿만 바뀌어 **was만 다시 빌드**되고 DB 변경은 없습니다 |

- 수동 배포는 감시 브랜치와 `v*` 태그만 고를 수 있습니다. 1차 데모 순서는 v1 → v2 → v1입니다.
- 2차 데모는 실제 로직 + LLM 기능(분석·AI 패치·인프라 생성)입니다. v2 이후 기능은 미정이며 로그인은 후보 중 하나입니다.
- `prod/...` 이름의 브랜치는 만들지 않습니다(`prod` 브랜치와 함께 둘 수 없습니다).

## 지킬 것

- **DB는 MySQL 8.4**(✅ 장부 12). 앱은 SQLAlchemy + `DATABASE_URL`로 연결합니다. 마이그레이션은 추가형만 쓰고 DB는 롤백하지 않으므로 v1 코드가 새 스키마에서도 동작해야 합니다.
- RDS 연결 TLS: 클라이언트가 CA를 **검증**해야 합니다. 앱은 RDS CA 번들(`certs/global-bundle.pem`)을 저장소에 포함하고 `DATABASE_URL`에 `ssl_ca`를 붙입니다(N24). `ssl_ca`가 있는데 검증된 TLS 연결이 아니면 `/health/ready`가 503입니다.
- **일부러 둔 개발값**(✅ 장부 13): 앱의 `dev.env`와 코드에 있는 `sqlite:///...`, `localhost`, `SECRET_KEY=dev` 같은 값은 데모용입니다. **고치지 않습니다.** 배포 때 파이프라인이 대상 값으로 바꿉니다. AI 코딩 도구에도 같은 규칙을 적용합니다(AGENTS.md).
- 쿠키 Secure·ProxyFix는 **클라우드만** 켭니다(`PROXY_FIX_*` 환경 키, 온프렘 0/0, 클라우드 2/1). `/version`은 DB 종류·가린 호스트·TLS 여부를 보여 줍니다.
- AI 패치(토글, 일반 실행 기본 OFF)는 승인된 diff를 `prod` 원본에 적용한 트리를 실행기가 `ai-prod` 커밋으로 고정합니다. 패치 검사는 `src/ddak/plan/patch`(O3)가 합니다.

## 검증과 기록

- 배포 뒤 검증은 `smoke_test`(`src/ddak/verify/smoke`)의 시나리오 묶음으로 합니다: `base`(v1 기능), `v2`(이미지·박스). v2 배포 run은 `["base", "v2"]`, v1으로 되돌린 run은 `["base"]`를 고릅니다.
- [`results/2026-10-03/`](results/2026-10-03/README.md): v1 → v2 → v1과 migrate 결함 DB 복구를 MySQL 8.4에서 실행한 러너·smoke 출력(파이프라인을 거치지 않은 로컬 실행).

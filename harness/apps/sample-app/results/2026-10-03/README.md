# 샘플 앱 러너·스모크 결과 샘플 — 2026-10-03 KST

샘플 앱(flaskr, [gerbera-application](https://github.com/2026-Gerbera/gerbera-application))을 데모 순서 **v1 → v2 → v1**로 바꿔 띄우고,
단계마다 마이그레이션 러너(`python -m flaskr.migrate precheck|up|verify`)와 `smoke_test`(base 묶음)를 돌린 **로컬 단일 실행 기록**이다.
파이프라인(감지·계획·승인·빌드·배포)을 거친 결과가 아니고, 실제 VM·AWS도 쓰지 않았다. 담당 장민영(O3).

| 단계 | 앱 | migrate (precheck → up → verify) | 스키마 서명(verify) | smoke base 5개 | smoke 시간(초) |
|---|---|---|---|---|---:|
| 1. v1 첫 배포 | 태그 `v1` | 빈 DB → `0001` 적용 → 통과 | `sha256:07ace2f785b3…` | 통과 | 0.232 |
| 2. v2 | 태그 `v2` | 새 마이그레이션 없음, 통과 | 같음 | 통과 | 0.246 |
| 3. v1 재배포 | 태그 `v1` | 통과(DB는 롤백하지 않음) | 같음 | 통과 | 0.156 |
| 4. 결함 DB 복구 | 태그 `v1` | 버전 행만 있고 표가 없는 DB → precheck가 `current=null` → up이 표를 다시 만듦 → 통과 | 같음(복구 전 `d82a3874…`) | – | – |

- smoke base = `S0.version`(release_id = run_id, DB mysql), `S0.ready`, `B1` 목록, `B2.create` 익명 글쓰기, `B2.empty` 빈 제목 오류.
- v2는 목록 페이지에 이미지·박스를 더한 1차 데모 변경이다. 템플릿만 바뀌어 was만 다시 빌드했고, web은 v1 이미지를 그대로 썼다.
  목록 HTML의 `<svg>` 수는 v1 0 / v2 1 / v1 재배포 0이었다.
- 4번은 10/2 P0(migrate가 버전 행만 보고 통과하던 결함) 수정의 확인이다. 실행기(onprem `migrate_db`)는
  precheck의 `current == expected`이면 up을 건너뛰므로, 수정 전에는 이 DB에서 up이 돌지 않고 verify도 통과했다.
- 전체 약 80초(MySQL 준비 뒤부터 4단계 끝까지, 앱 기동 고정 대기 4초×3 포함, 이미지 빌드 제외).

## 파일

| 파일 | 내용 |
|---|---|
| `<단계>.migrate.txt` | 러너 stdout의 `MIGRATE_RESULT` 줄(precheck·up·verify). C-09 형식 |
| `<단계>.stderr.txt` | 러너 진단 문구(stderr) |
| `<단계>.smoke.json` | `smoke_test` 출력(C-10 형식, `source=live`) |
| `4-repair.db-after.txt` | 복구 뒤 표 목록과 `schema_migrations` |
| `timing.txt` | 전체 시간, 라이브러리·MySQL 버전 |

결과에는 주소·쿠키 값·비밀값이 없다(smoke 결과 규칙). 접속 문자열은 기록하지 않았다.

## 비교할 때 유지할 조건

- 개발 PC(Windows 11, Docker Desktop) 한 대. 컨테이너 네트워크 하나에 MySQL 8.4.11(공식 이미지 digest `6ea90827b110`), was, web.
  온프렘 VM·Cloudflare Tunnel·TLS 없이 `http://web:8080`으로 접속했다. `db.tls=true, tls_verified=false`는 MySQL 기본 TLS(검증 없음)이다.
- was 이미지: 앱 커밋 `04d779d`(= 태그 `v1`)와, `feature/image-box`를 그 위로 rebase한 트리(태그 `v2` = `adcafb9`와 같은 변경)에서
  `docker/was.Dockerfile`로 직접 빌드했다(CodeBuild 아님, 단일 플랫폼). web 이미지는 v1 커밋에서 빌드한 것으로, `nginx/`·`docker/web.Dockerfile`·`flaskr/static/`은 `04d779d`와 같다.
- 라이브러리: Flask 3.1.3, SQLAlchemy 2.1.1, PyMySQL 2.2.8. smoke는 팀 저장소 `verify/smoke`를 `ddak-ci` 컨테이너(python 3.13, uv)에서 실행했다.
- MySQL root 계정으로 마이그레이션·앱을 같이 돌렸다. 온프렘의 앱 계정(DML)·마이그레이터 계정(DDL) 분리는 반영하지 않았다.
- 1회 실행 값이다. smoke 시간은 같은 호스트 안 HTTP라 실제 환경 시간과 비교할 수 없다.
- 4번의 DB 손상은 `DROP TABLE post`로 흉내 냈다. 실행기 판단(precheck 결과로 up 생략)은 같은 순서로 러너를 부른 것이지 실행기 코드를 거친 것이 아니다.

## 5. 실패 주입 (`5-failinject.txt`, 10/3 추가)

O3 역할 문서의 "마이그레이션을 건너뛰면 `/health/ready`가 503" 확인이다. 지금 v2(이미지·박스)에는 새 마이그레이션이 없어서,
v2 was 이미지에 시험용 `0002`(추가형 표 하나) 파일만 얹은 임시 이미지로 확인했다(앱 저장소에는 넣지 않음).

| 상황 | `/health/ready` | 목록 `/` | precheck `current` |
|---|---|---|---|
| 이미지 `0002`, DB `0001`(migrate 건너뜀) | **503**(current 0001 < expected 0002) | – | `0001` → 실행기가 up 실행 |
| up·verify 뒤 | 200 | – | – |
| 버전 행은 있는데 표(`post`)가 없는 결함 DB | **200**(버전 행만 봄) | **500** | `null` → up이 복구 |

- 확인됨: 새 마이그레이션을 적용하지 않고 배포하면 ready가 503이라 배포가 실패로 드러난다.
- 한계: ready는 `schema_migrations` 최대 버전만 보므로 표가 사라진 DB를 통과시킨다. migrate가 도는 run은 precheck(행+객체 기준)가
  잡아 up이 복구하지만, 새 마이그레이션이 없는 run(v2 등)은 migrate를 돌리지 않으므로 smoke `B1`(목록)이 잡아야 한다.
  ready에 객체 확인을 넣을지는 앱 변경이라 팀과 정한다(ready는 자주 불리므로 비용도 함께 본다).

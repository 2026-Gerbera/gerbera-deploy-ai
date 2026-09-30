# gerbera-on-premise

# C3 클라우드 검증 및 관리 페이지

C3는 클라우드 배포 결과 검증, 배포 결과 보고, FastAPI 관리 페이지 구현

## 구현 내용

### AWS 클라우드 상태 검증

- boto3 읽기 전용 프로필을 이용한 AWS 연결
- ECS 실행 태스크 조회
- ECS 컨테이너의 실제 `imageDigest` 확인
- 배포 이미지의 플랫폼별 digest 비교
- ALB Target Group의 healthy 상태 확인
- 애플리케이션 `/health/ready` 응답 확인
- `/version`의 release ID 확인

주요 파일:

- `src/ddak/cloud/health/aws.py`
- `src/ddak/cloud/health/health.py`
- `src/ddak/cd/tools/health_check/tool.py`

### TLS 및 HTTPS 검증

- 도메인과 ALB DNS 연결 확인
- 인증서 체인과 호스트명 검증
- TLS 1.2/1.3 협상 여부 확인
- 인증서 발급자와 남은 유효기간 확인
- ACM 인증서 발급 및 ALB 연결 상태 확인
- ALB HTTP/HTTPS listener와 보안 정책 확인
- HTTP에서 HTTPS로의 301 redirect 확인
- HSTS 헤더 확인
- V1~V9 검증 결과를 `pass`, `fail`, `inconclusive`로 반환

주요 파일:

- `src/ddak/cloud/health/tls.py`
- `src/ddak/cloud/health/tool.py`
- `src/ddak/cloud/health/fake.py`

### 클라우드 Provider 모듈 분리

- AWS, GCP, Azure 배포 provider를 개별 모듈로 분리
- AWS, GCP, Azure health/TLS provider를 개별 모듈로 분리
- 공통 `CdProvider` 인터페이스 적용
- provider 이름을 이용한 클라우드 provider 선택 기능 추가
- 아직 구현되지 않은 GCP/Azure 기능이 AWS 코드로 실행되지 않도록 차단
- 기존 AWS provider import 경로 호환 유지

주요 파일:

- `src/ddak/cd/interface.py`
- `src/ddak/cloud/deploy/providers/`
- `src/ddak/cloud/health/providers/`

현재 실제 클라우드 검증은 AWS를 기준으로 구현되어 있으며, GCP와 Azure는 provider 구조와
미지원 처리까지 구현되어 있다.

### 배포 결과 보고

- 클라우드 도메인과 이미지 산출물 누락 검사
- 규칙 기반 이슈 카드 생성
- 이미지 index digest와 플랫폼별 digest 정리
- Fake 결과 보고 지원
- `post_report` 도구 등록

주요 파일:

- `src/ddak/verify/report/rules.py`
- `src/ddak/verify/report/logic.py`
- `src/ddak/verify/report/fake.py`
- `src/ddak/verify/report/tool.py`

### FastAPI 관리 페이지

- 프로젝트 배포 대시보드
- 클라우드 도메인과 DNS 설정 화면
- 배포 항목 통합 승인 및 거절
- 승인 대상 해시와 패치 diff 표시
- 승인된 패치 다운로드
- 온프레미스와 클라우드 2열 진행 화면
- SSE를 이용한 실시간 실행 이벤트 표시
- 연결이 끊어진 경우 저장된 이벤트 replay
- 배포 상태와 단계별 실행 시간 결과 화면

주요 파일:

- `src/ddak/web/app.py`
- `src/ddak/web/routes/`
- `src/ddak/web/templates/`
- `src/ddak/web/static/`

### 관리 페이지 보안

- 관리 페이지를 `127.0.0.1`에만 바인드
- POST 요청의 Host와 Origin 검사
- SameSite Strict CSRF 쿠키와 토큰 검증
- Jinja 기본 HTML 이스케이프 적용
- SSE 이벤트와 승인 데이터의 비밀값 마스킹
- 코드 수정 토글이 꺼진 상태에서 패치 실행 차단

주요 파일:

- `src/ddak/web/security.py`
- `src/ddak/core/redact.py`
- `src/ddak/executor/service.py`

### 도메인 설정 검증

- 도메인 소문자 변환과 마지막 점 제거
- 한글 도메인의 IDNA 변환
- URL, 경로, 포트, IP 주소, wildcard 입력 차단
- 도메인 전체 길이와 label 길이 검증
- AWS 기본 도메인 입력 차단
- External DNS의 apex 도메인 입력 차단
- Route53 hosted zone ID 형식 검증

주요 파일:

- `src/ddak/web/domain.py`
- `src/ddak/web/routes/settings.py`

### 프로젝트 설정 저장

- SQLite 기반 프로젝트 설정 저장 및 조회
- `cloud_domain`, `dns_mode`, `hosted_zone_id` 저장
- 설정 version 관리
- 낙관적 버전 검사를 이용한 동시 수정 충돌 방지
- 수정자와 수정 시각 기록

주요 파일:

- `src/ddak/core/store.py`

### 공통 입출력 계약

- `health_check` 입출력 모델
- `verify_tls` 입출력 모델
- `post_report` 입출력 모델
- 각 계약의 JSON Schema

주요 파일:

- `src/ddak/core/contracts/tools/health_check.py`
- `src/ddak/core/contracts/tools/verify_tls.py`
- `src/ddak/core/contracts/tools/post_report.py`
- `harness/contracts/schemas/`

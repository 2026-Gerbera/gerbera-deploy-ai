# cloud/tls — 정준우

`ensure_tls("check", ctx)`는 읽기 전용 AWS 세션으로 ACM 발급·유효기간·도메인·ALB 연결과 443 인증서/TLS 정책, 80→HTTPS 301을 확인한다. 이어 인증서 검증을 켠 TLS 1.2 이상 HTTPSConnection으로 443 HEAD 응답을 확인한다. HTTP 상태 성공·앱 기능·HSTS는 C3 `verify_tls`/health/smoke가 담당한다.

`ctx.platform["cloud"]`의 `alb_arn`, `certificate_arn`, `https_listener_arn`, `http_listener_arn`을 읽는다. PR #1의 C3 구현과 같은 이름이며, 공통 C-03 확정은 별도다. 프로필은 `DDAK_AWS_READONLY_PROFILE` 또는 `ddak-readonly`; 쓰기 프로필 fallback은 없다.

10/1 사용자 결정으로 TLS 담당은 정준우, ACM·443·HTTP→HTTPS 리다이렉트·HSTS 생성은 플랫폼 Terraform 소유로 확정됐다. `ensure_tls`는 발급 완료·443 동작을 확인하고 불충족 시 실패해야 하며 직접 발급·변경하지 않는다. 현재 `apply` 모드는 차단되며 공유 계약의 모드 정리는 별도 조율한다. CD ensure_tls를 등록했고 ddak.app에서 C1 공개 함수를 주입한다. C2 AwsProvider 파일은 수정하지 않았고 그 직접 메서드는 아직 미구현이다.

443 probe는 별도 자식 프로세스에서 DNS부터 HEAD까지 실행하며 monotonic 기준 min(10초, 남은 step 시간) 안에 끝낸다. 시간 초과 시 자식을 종료·회수하고 passed=False로 반환한다(ADAPTER_TIMEOUT/NEEDS_HUMAN 아님). 테스트는 가짜 AWS·HTTPSConnection 및 로컬 자식의 DNS 지연 주입이다. 실제 공개 443 접속은 야간에 실행하지 않았다. 플랫폼 HSTS 생성/실환경 검증은 팀 통합이 필요하다.

FAKE 컨텍스트로 이 실제 AWS 구현을 직접 부르면 세션 생성 전에 거절한다.

리다이렉트는 원래 path/query를 보존해야 한다. 80 리스너는 기본 규칙 하나만 허용하며 추가 규칙·페이지가 있으면 통과시키지 않는다. 세 조회(인증서·리스너·규칙)의 connect/read 예산을 분배하고 조회 사이·후에 deadline을 검사한다. AWS SDK 자격증명 공급자/DNS 지연 전체를 강제 종료하는 기능은 아니다. 별도 443 probe의 DNS에는 위 10초 상한이 적용된다. 443 뒤 실제 앱 라우팅·응답은 C3 확인 범위다.

# cloud/tls — 정준우

`ensure_tls("check", ctx)`는 읽기 전용 AWS 세션으로 ACM 발급·유효기간·도메인·ALB 연결과 443 인증서/TLS 정책, 80→HTTPS 301을 확인한다. 실제 DNS·HTTPS·HSTS 요청은 C3 `verify_tls`가 담당한다.

`ctx.platform["cloud"]`의 `alb_arn`, `certificate_arn`, `https_listener_arn`, `http_listener_arn`을 읽는다. PR #1의 C3 구현과 같은 이름이며, 공통 C-03 확정은 별도다. 프로필은 `DDAK_AWS_READONLY_PROFILE` 또는 `ddak-readonly`; 쓰기 프로필 fallback은 없다.

10/1 사용자 결정으로 TLS 담당은 정준우, ACM·443·HTTP→HTTPS 리다이렉트·HSTS 생성은 플랫폼 Terraform 소유로 확정됐다. `ensure_tls`는 발급 완료·443 동작을 확인하고 불충족 시 실패해야 하며 직접 발급·변경하지 않는다. 현재 `apply` 모드는 차단되며 공유 계약의 모드 정리는 별도 조율한다. 현재 AwsProvider 위임/공유 툴 등록은 PR #1 보호 범위라 수정하지 않았다.

현재 구현은 AWS 설정 조회까지다. 실제 443 접속 동작 확인과 플랫폼 HSTS 설정 연결은 남은 C1 작업이다. 이번 결정 기록을 구현 완료로 해석하지 않는다.

FAKE 컨텍스트로 이 실제 AWS 구현을 직접 부르면 세션 생성 전에 거절한다.

리다이렉트는 원래 path/query를 보존해야 한다. 80 리스너는 기본 규칙 하나만 허용하며 추가 규칙·페이지가 있으면 통과시키지 않는다. 세 조회(인증서·리스너·규칙)의 connect/read 예산을 분배하고 조회 사이·후에 deadline을 검사한다. SDK 자격증명 공급자/DNS 지연까지 강제 종료하는 기능은 아니다. 443 뒤 실제 앱 라우팅·응답은 C3 확인 범위다.

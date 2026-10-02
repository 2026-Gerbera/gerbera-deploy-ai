# onprem/inventory (담당: 김준석)

- `load_inventory(path)`: YAML을 검증하여 `RunContext.platform["onprem"]`용 dict를 반환한다. flow가 컨텍스트에 넣는다.
- tier·SSH·readiness·공개 설정·MySQL 볼륨 보호 검증은 `onprem/deploy`의 공개 `InventoryConfig`·`TierConfig`를 재사용한다. 모델 필드를 복사하지 않는다.
- 파일 로더는 tier 이름과 `docker_host`의 `unix://` 제한을 추가한다. VM은 `mode: vm`과 전체 또는 tier별 고정 지문 SSH 설정으로 받는다.
- 비밀값은 넣지 않는다. `key_path`·`host_key_fingerprint`는 허용된 접속 메타데이터이며, password/token 등 미정의 필드와 허용 목록 밖의 public_env는 거부한다. 검증 오류에는 입력값을 출력하지 않는다.
- DB는 `kind: mysql`과 `/var/lib/mysql`에 연결된 단일 쓰기 가능한 named volume이 필요하다.
- AI 호출 금지(import-linter 계약).

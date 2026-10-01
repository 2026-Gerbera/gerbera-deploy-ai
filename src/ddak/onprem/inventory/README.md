# onprem/inventory (담당: 김준석)
- `load_inventory(path)`: `platform.onprem.yaml`을 읽어 `RunContext.platform["onprem"]` 모양(`docker_host`, `tiers`)으로 돌려준다. flow가 컨텍스트에 넣는다.
- 거부: 파일 없음, 최상위 비매핑, tier 이름 위반, 시크릿 키(password/secret/token/key), 비로컬 `docker_host`(`unix://`만), 알 수 없는 필드. 값 상세 검증은 `onprem/deploy` provider가 한다.
- 모양: `src/ddak/onprem/deploy/provider.py` docstring의 인벤토리 계약.
- AI 호출 금지(import-linter 계약).

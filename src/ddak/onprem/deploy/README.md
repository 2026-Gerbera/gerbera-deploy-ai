# onprem/deploy (담당: 정준우)
- 할 일: 온프렘 Docker 배포·롤백·설정 주입·마이그레이션. containers.py(옛 cd/docker_host.py), provider.py(옛 cd/providers/onprem.py), `__init__.py`가 OnPremProvider(CD 인터페이스 구현) 노출
- 입출력 계약: `src/ddak/core/contracts`, CD 인터페이스는 `src/ddak/cd/interface.py`(ProviderResult)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).

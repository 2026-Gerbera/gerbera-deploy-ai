"""onprem: 온프렘 팀 디렉토리. 하위 디렉토리가 담당 경계다.

- deploy/ 정준우(O1): Docker 배포(containers.py, provider.py),
  OnPremProvider(CD 인터페이스 구현) 노출
- provision/ 김준석(O2): 온프렘 서버·컨테이너 준비, 앱 DB·계정
- inventory/ 김준석(O2): 인벤토리(tier별 주소) 읽기
AI를 import하지 않는다(import-linter 계약 1).
"""

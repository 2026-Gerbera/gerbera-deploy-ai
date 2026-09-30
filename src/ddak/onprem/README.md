# onprem (온프렘 팀: 정준우·김준석)
- 할 일: deploy/(정준우), provision/·inventory/(김준석). 각 하위 README를 본다.
- 입출력 계약: `src/ddak/core/contracts`, CD 인터페이스는 `src/ddak/cd/interface.py`
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(onprem은 전부 금지).

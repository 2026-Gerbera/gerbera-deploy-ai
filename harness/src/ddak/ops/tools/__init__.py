"""툴 패키지. 1툴 1디렉토리: tools/<tool_name>/{__init__,tool,logic,local,cloud,fake}.py
(AI 툴은 prompt.md 추가).

ddak.app이 이름순으로 자동 등록한다. 이름이 '_'로 시작하는 디렉토리는 제외한다.
새 툴은 ddak/core/tools/ping/을 복사해서 시작한다.
"""

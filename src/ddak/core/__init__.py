"""ddak 공용 코어: 계약 모델, 툴 레지스트리, 어댑터 규약, AI 관문(call_ai), redact, 로그.

여기서는 하위 모듈을 import하지 않는다. 특히 ddak.core.ai는 절대 여기서 불러오지 않는다
(import-linter 계약 2: AI 관문은 허용된 AI 툴 모듈과 조립 진입점 ddak.app만 import한다).
"""

"""replay backend(테스트·비상): 저장된 응답을 돌려준다. 결과에 source=replay 라벨이 붙는다.

- 위치: <DDAK_AI_REPLAY_DIR>/<purpose>/<키>.json (기본 fixtures/ai_replay/)
- 키: purpose, prompt_version, system, user(redact 후), json_schema의 정규화 JSON sha256.
  입력이 한 글자라도 바뀌면 키가 바뀐다(결정적).
- 파일에는 출력과 메타정보만 저장한다. 입력 원문은 저장하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ddak.core.ai.providers import AIRequest, AIResponse
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode


def replay_key(req: AIRequest) -> str:
    payload = {
        "purpose": req.purpose,
        "prompt_version": req.prompt_version,
        "system": req.system,
        "user": req.user,
        "schema": dict(req.json_schema),
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReplayProvider:
    name = "replay"

    def __init__(self, root: Path) -> None:
        self._root = root

    def path_for(self, req: AIRequest) -> Path:
        return self._root / req.purpose / f"{replay_key(req)}.json"

    def complete(self, req: AIRequest) -> AIResponse:
        path = self.path_for(req)
        if not path.is_file():
            short = replay_key(req)[:12]
            raise DdakToolError(
                ErrorCode.AI_UNAVAILABLE, f"저장된 응답 없음: {req.purpose}/{short}"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            output = data["output"]
        except (OSError, ValueError, KeyError, TypeError):
            raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "저장된 응답 형식 오류") from None
        return AIResponse(text=json.dumps(output, ensure_ascii=False), source=Source.REPLAY)

    def save(self, req: AIRequest, output: Mapping[str, Any]) -> Path:
        """live 응답을 저장해 두는 도우미(리허설 녹화용, 사람이 부른다)."""
        path = self.path_for(req)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "purpose": req.purpose,
            "prompt_version": req.prompt_version,
            "output": dict(output),
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return path

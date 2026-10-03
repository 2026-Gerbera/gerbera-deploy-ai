"""기존 LLM 상태 callback 호환. 웹은 app이 주입한 callback만 쓴다."""

from __future__ import annotations

from typing import Any

from ddak.core.ai.providers import provider_model, provider_status, selected_provider_id
from ddak.core.config import Settings


def llm_status(settings: Settings | None = None) -> dict[str, Any]:
    cfg = settings or Settings.from_env()
    try:
        id = selected_provider_id(cfg)
        status = provider_status(id, cfg)
        model = provider_model(cfg)
    except Exception:
        id = "unknown"
        model = None
        status = {"status": "red", "detail": "AI provider 설정 오류"}
    return {
        **status,
        "provider": id,
        "backend": cfg.llm_backend.value,
        "ok": status["status"] == "green",
        "model": model,
        "groq_key": bool(cfg.groq_api_key),
        "jev_key": bool(cfg.jev_api_key) or cfg.jev_key_configured,
        "jev_backend": cfg.selected_provider("judgment"),
    }

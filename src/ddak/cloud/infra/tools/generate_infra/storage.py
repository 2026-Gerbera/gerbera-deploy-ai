"""app UPDATE의 저장소 create 초안과 remove 빈 번들을 만든다."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator

from ddak.cloud.infra.storage_bundle import (
    MAX_RATIONALE_BYTES,
    STORAGE_PROMPT_VERSION,
    load_storage_baseline,
    storage_bundle,
    storage_request,
    storage_source,
)
from ddak.core.config import Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.enums import Source
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.tools.generate_infra import GenerateInfraInput, GenerateInfraOutput

from .logic import TerraformDraft, TerraformFileDraft, _bounded_ai_data, _draft_files, _write_bundle

_PROMPT = (Path(__file__).parent / "prompt_storage.md").read_text(encoding="utf-8")


class StorageDraft(TerraformDraft):
    files: tuple[TerraformFileDraft, ...] = Field(min_length=1, max_length=1)
    rationale: tuple[str, ...] = Field(min_length=5, max_length=5)

    @field_validator("rationale")
    @classmethod
    def bounded_steps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item.strip()
            or len(item.encode("utf-8")) > MAX_RATIONALE_BYTES
            or "\n" in item
            or "\r" in item
            for item in value
        ):
            raise ValueError("저장소 근거는 단계마다 240바이트 이하 한 문장이어야 한다")
        return value


def generate_storage(
    inp: GenerateInfraInput, ctx: RunContext, *, ai: Callable[..., Any]
) -> GenerateInfraOutput:
    import json

    from ddak.core.storage import OUTPUT_KEY

    intent, binding, evidence = storage_request(ctx)
    original = Path(inp.directory)
    directory = original.resolve()
    if not directory.is_dir() or original.is_symlink() or any(directory.iterdir()):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "비어 있는 인프라 번들 디렉터리가 필요하다")
    if intent == "remove":
        rationale = (
            "현재 소스 분석은 업로드 저장소 제거를 요청한다.",
            "기존 업로드 버킷을 제거하는 app 갱신을 준비한다.",
            "빈 리소스 번들로 저장소 리소스 4개의 삭제 plan을 만든다.",
            "근거와 삭제 계획을 사람에게 보여 승인을 받는다.",
            "승인 후 적용하고 upload_bucket 출력과 저장소 주입을 정리한다.",
        )
        source = Source.FIXTURE if ctx.adapter_mode.value == "fake" else Source.LIVE
        rendered = storage_bundle("# 업로드 저장소 리소스를 제거합니다.\n", ctx, rationale, source)
    else:
        rendered = load_storage_baseline(directory.parent.parent, ctx)
        if rendered is not None:
            source = Source.CACHE
        else:
            # 소스 본문·값·state·plan·다른 설정은 입력에 넣지 않는다.
            data = json.dumps({**binding, "evidence": evidence}, ensure_ascii=False)
            result = ai(
                instruction=_PROMPT,
                data=_bounded_ai_data(data),
                output_model=StorageDraft,
                prompt_version=STORAGE_PROMPT_VERSION,
                settings=replace(Settings.from_env(), ai_timeout_s=240),
            )
            draft = StorageDraft.model_validate(result.value)
            files = _draft_files(draft)
            if set(files) != {"storage.tf"}:
                raise DdakToolError(ErrorCode.AI_OUTPUT_INVALID, "저장소 파일 이름은 storage.tf다")
            source = Source(storage_source(result.source))
            rendered = storage_bundle(files["storage.tf"], ctx, draft.rationale, source)
    files = _write_bundle(directory, rendered)
    return GenerateInfraOutput(
        directory=str(directory),
        layer="app",
        files=files,
        outputs={OUTPUT_KEY: ("aws_s3_bucket.uploads.id", "string")} if intent == "create" else {},
        source=source,
    )

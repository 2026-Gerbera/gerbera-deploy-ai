"""결정적 규칙 카드. 실행 성공·실패 판정은 실행기 결과를 그대로 사용한다."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ddak.core.ai.gateway import call_ai
from ddak.core.answer_language import with_answer_language
from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.post_report import PostReportInput, PostReportOutput, ReportNarrative
from ddak.core.redact import redact_obj
from ddak.verify.report.rules import issues_for


def post_report(inp: PostReportInput, ctx: RunContext) -> PostReportOutput:
    artifacts = ctx.release_artifacts
    indexes = (
        {tier: image.index_digest for tier, image in artifacts.images.items()} if artifacts else {}
    )
    platforms = (
        {tier: dict(image.platform_digests) for tier, image in artifacts.images.items()}
        if artifacts
        else {}
    )
    issues = issues_for(ctx)
    summary = "배포 실행 결과가 기록되었습니다"
    if issues:
        summary = f"배포 실행 결과가 기록되었습니다. 확인할 항목 {len(issues)}개"
    report = PostReportOutput(
        run_id=inp.run_id,
        summary=summary,
        issues=issues,
        image_index_digests=indexes,
        platform_digests=platforms,
    )

    if inp.facts is None:
        return report
    facts = redact_obj(inp.facts.model_dump(mode="json"))
    failed = [s for s in facts["steps"] if s["status"] in {"failed", "check_failed"}]
    conclusion = (
        "배포와 검증을 완료했습니다."
        if facts["status"] == "SUCCEEDED"
        else "배포를 완료하지 못했습니다. 환경별 결과를 확인하세요."
    )
    fallback = ReportNarrative(
        conclusion=conclusion,
        changes=[
            f"변경 파일 {len(facts['files'])}개를 기록했습니다.",
            f"이미지 {len(facts['images'])}개의 배포 정보를 기록했습니다.",
        ],
        checks=[f"검증 기록 {len(facts['checks'])}개 · 실패 단계 {len(failed)}개"],
        next_action="서비스 주소에서 배포 버전을 확인하세요."
        if not failed and facts["status"] == "SUCCEEDED"
        else "첫 실패 단계와 환경별 복구 결과를 확인한 뒤 새 배포를 준비하세요.",
    )
    report = report.model_copy(update={"summary": conclusion, "narrative": fallback})
    if ctx.adapter_mode is AdapterMode.FAKE:
        return report
    try:
        result = call_ai(
            instruction=with_answer_language(
                Path(__file__).with_name("prompt.md").read_text(),
                ctx.project_settings.get("ai_answer_language", "ko"),
            ),
            data=json.dumps(facts, ensure_ascii=False),
            output_model=ReportNarrative,
            prompt_version="report-v3",
            settings=replace(Settings.from_env(), ai_timeout_s=20, ai_retries=0),
        )
        narrative = ReportNarrative.model_validate(redact_obj(result.value.model_dump()))
        return report.model_copy(
            update={"summary": narrative.conclusion, "narrative": narrative, "source": "ai"}
        )
    except Exception:
        return report

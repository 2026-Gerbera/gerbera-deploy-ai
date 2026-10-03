"""배포 봉인 이후의 설명 작업. run·release 상태는 쓰지 않는다."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path

from ddak.core.contracts.tools.post_report import PostReportInput, PostReportOutput, ReportFacts
from ddak.core.redact import redact_obj
from ddak.core.runlog import run_dir
from ddak.core.runtime import tool_context

SUMMARY_TIMEOUT = 20.0


def report_facts(run: dict, release: dict, display: dict) -> ReportFacts:
    records = (run.get("result") or {}).get("steps", {})
    steps = []
    checks = []
    for sid, row in records.items():
        code = re.search(
            r"\b(?:ADAPTER_FAILED|CONFIG_INVALID|TIMEOUT|PRECONDITION_FAILED|INTERNAL|AI_UNAVAILABLE)\b",
            row.get("error") or "",
        )
        step = {
            "id": sid,
            "status": row.get("status", "unknown"),
            "elapsed_s": row.get("elapsed_s"),
            "error_code": code[0] if code else None,
        }
        steps.append(step)
        if sid.startswith("verify.") and sid != "verify.report":
            checks.append(step)
            for scenario in (row.get("output") or {}).get("scenarios", []):
                checks.append(
                    {
                        "id": scenario.get("id", "unknown"),
                        "status": "succeeded" if scenario.get("ok") else "check_failed",
                    }
                )
    images = []
    deployed = {}
    for environment in release.get("environment_images", {}).values():
        deployed.update(environment.get("images", {}))
    if not deployed and run["status"] == "SUCCEEDED":
        deployed = release.get("images", {})
    for tier, ref in deployed.items():
        match = re.search(r"sha256:([a-f0-9]{64})", ref)
        if match:
            images.append({"tier": tier, "digest_prefix": match[1][:12]})
    return ReportFacts.model_validate(
        redact_obj(
            {
                "status": run["status"],
                "steps": steps,
                "files": [
                    {"file": name, "tier": tier, "change": change}
                    for name, tier, change in sorted(
                        {(f["file"], f["tier"], f["change"]) for f in display.get("files", [])}
                    )
                ],
                "findings": [
                    {
                        "file": f["file"],
                        "line": f.get("line"),
                        "kind": f.get("pattern_id") or "환경 의존 설정",
                    }
                    for f in display.get("findings", [])
                ],
                "images": images,
                "checks": checks,
            }
        )
    )


def fallback(facts: ReportFacts) -> dict:
    success = facts.status == "SUCCEEDED"
    return {
        "state": "ready",
        "source": "rule",
        "narrative": {
            "conclusion": "배포와 검증을 완료했습니다."
            if success
            else "배포를 완료하지 못했습니다. 환경별 결과를 확인하세요.",
            "changes": [
                f"변경 파일 {len(facts.files)}개를 기록했습니다.",
                f"이미지 {len(facts.images)}개의 배포 정보를 기록했습니다.",
            ],
            "checks": [f"검증 기록 {len(facts.checks)}개"],
            "next_action": "서비스 주소에서 배포 버전을 확인하세요."
            if success
            else "첫 실패 단계와 환경별 복구 결과를 확인한 뒤 새 배포를 준비하세요.",
        },
    }


def save(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(redact_obj(value), ensure_ascii=False))
    temp.replace(path)


class Reports:
    def __init__(self, service):
        self.service = service
        self.tasks: dict[str, asyncio.Task] = {}

    def path(self, run_id: str) -> Path:
        return run_dir(self.service.root / "runs", run_id) / "report-summary.json"

    def get(self, run_id: str) -> dict | None:
        try:
            data = json.loads(self.path(run_id).read_text())
        except (OSError, ValueError):
            return None
        if (
            data.get("state") == "pending"
            and time.time() - data.get("started", 0) >= SUMMARY_TIMEOUT
        ):
            return data["fallback"]
        return data

    def start(self, run_id: str, context) -> None:
        run = self.service.get_run(run_id)
        facts = report_facts(
            run, self.service.get_release(run_id) or {}, self.service.get_display_data(run_id)
        )
        rule = fallback(facts)
        path = self.path(run_id)
        if "post_report" not in self.service.registry.registered():
            save(path, rule)
            return
        save(path, {"state": "pending", "started": time.time(), "fallback": rule})
        self.tasks[run_id] = asyncio.create_task(self._generate(run_id, context, facts, rule))

    async def _generate(self, run_id, context, facts, rule) -> None:
        value = rule
        started = time.monotonic()
        try:
            registered = self.service.registry.get("post_report")
            inp = registered.input_model.model_validate(
                PostReportInput(run_id=run_id, facts=facts).model_dump()
            )
            with tool_context("post_report", run_id):
                operation = (
                    registered.fn(inp, context)
                    if registered.is_async
                    else asyncio.to_thread(registered.fn, inp, context)
                )
                result = await asyncio.wait_for(operation, timeout=SUMMARY_TIMEOUT)
            report = PostReportOutput.model_validate(result.model_dump())
            if report.narrative:
                value = {
                    "state": "ready",
                    "source": report.source,
                    "narrative": report.narrative.model_dump(),
                }
        except (Exception, asyncio.CancelledError):
            value = rule
        value["elapsed_s"] = round(time.monotonic() - started, 2)
        with contextlib.suppress(OSError):
            save(self.path(run_id), value)

    async def shutdown(self) -> None:
        pending = [task for task in self.tasks.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

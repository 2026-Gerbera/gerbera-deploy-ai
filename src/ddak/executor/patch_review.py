"""선택·재검토 초안. 제안 생성과 재계획은 조립부가 주입하며 승인 원본은 덮어쓰지 않는다."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import replace
from functools import partial
from typing import Any

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.contracts.patch_review import CodeProposal
from ddak.core.patch_ledger import PatchLostError
from ddak.core.redact import redact
from ddak.core.snapshots import digest_json, file_manifest

BUSY = {"generating", "reviewing", "finalizing", "publishing"}


class PatchReviews:
    def __init__(
        self, service: Any, *, generate: Any, combine: Any, finalize: Any, diff: Any
    ) -> None:
        self.service = service
        self.generate, self.combine, self.finalize, self.diff = generate, combine, finalize, diff
        self.tasks: dict[str, asyncio.Task] = {}

    def get(self, run_id: str) -> dict | None:
        return self.service.store.patch_review(run_id)

    def _prepared(self, run_id: str):
        p = self.service._load_prepared(run_id)
        if self.service.get_run(run_id)["status"] != "AWAITING_APPROVAL":
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "승인 대기 상태에서만 검토할 수 있습니다"
            )
        self.service._check_meta(p)
        settings = self.service.get_project_settings(p.context.project) or {}
        if settings.get("version", 0) != p.context.project_settings.get("version", 0):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "프로젝트 설정이 바뀌었습니다. 다시 준비하세요"
            )
        if digest_json(file_manifest(p.source)) != p.snapshot.source_snapshot_hash:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "원본 코드가 바뀌었습니다. 다시 준비하세요"
            )
        return p

    def _current(self, run_id: str, revision: int, *, busy: bool = False) -> dict:
        self._prepared(run_id)
        current = self.get(run_id)
        if current is None or current["revision"] != revision:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "검토 내용이 바뀌었습니다. 최신 화면을 확인하세요"
            )
        if not busy and current["state"] in BUSY:
            raise DdakToolError(
                ErrorCode.LOCK_HELD, "요청을 처리 중입니다. 완료 후 다시 시도하세요"
            )
        if current["state"] in {"cancelled", "complete"}:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "끝난 검토입니다. 최신 승인 화면을 확인하세요"
            )
        if current["state"] == "patch_lost":
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED,
                "이전 승인 수정이 유지되지 않았습니다. 다시 준비하세요",
            )
        return current

    @staticmethod
    def proposals(data: dict) -> list[CodeProposal]:
        return [CodeProposal.model_validate(p) for p in data["proposals"]]

    def _save(self, run_id: str, current: dict, **updates: Any) -> dict:
        return self.service.store.save_patch_review(
            run_id, current["revision"], {**current, **updates}
        )

    def begin(self, run_id: str) -> None:
        p = self._prepared(run_id)
        old = self.get(run_id)
        if old is not None and old["state"] not in {"cancelled", "sealed"}:
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "이미 코드 수정 검토가 있습니다")
        seed = {
            "revision": old["revision"] if old else 0,
            "state": "generating",
            "proposals": [],
            "selected": [],
            "candidate": None,
            "source": None,
            "notes": {},
            "error": None,
            "warnings": [],
            "successor": None,
            "source_sha": p.context.source_sha,
            "source_snapshot_hash": p.snapshot.source_snapshot_hash,
        }
        current = self._save(run_id, seed)
        self._launch(run_id, current, "generate")

    def request(
        self,
        run_id: str,
        revision: int,
        action: str,
        selected: list[str],
        *,
        prompt: str = "",
        candidate_id: str = "",
        notes: dict[str, str] | None = None,
    ) -> None:
        current = self._current(run_id, revision)
        if current["state"] == "sealed":
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "완료된 승인 자료입니다. 새 검토를 시작하세요"
            )
        proposals = self.proposals(current)
        ids = {p.id for p in proposals}
        if len(set(selected)) != len(selected) or set(selected) - ids:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "선택한 제안을 찾을 수 없습니다")
        if notes is not None:
            if set(notes) - ids or any(len(value) > 2000 for value in notes.values()):
                raise DdakToolError(
                    ErrorCode.CONFIG_INVALID, "수정 요청은 제안별 2000자까지 입력할 수 있습니다"
                )
            current = {**current, "notes": {key: redact(value) for key, value in notes.items()}}
        if action != "cancel" and any(
            item.required and item.id not in selected for item in proposals
        ):
            raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "이전 승인 수정은 유지해야 합니다")
        if action == "cancel":
            self._save(run_id, current, state="cancelled", candidate=None, error=None)
            return
        if action in {"adopt", "keep"}:
            candidate = current.get("candidate")
            if not candidate or candidate["id"] != candidate_id:
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "재검토 결과가 바뀌었습니다")
            if action == "adopt":
                proposal = CodeProposal.model_validate(candidate["proposal"])
                proposals = [proposal if p.id == proposal.id else p for p in proposals]
                self.combine(self._prepared(run_id).source, proposals, [p.id for p in proposals])
            sources = dict(current.get("proposal_sources", {}))
            if action == "adopt":
                sources[proposal.id] = candidate["source"]
            self._save(
                run_id,
                current,
                state="ready",
                proposals=[p.model_dump(mode="json") for p in proposals],
                selected=selected,
                candidate=None,
                error=None,
                proposal_sources=sources,
                source=next(
                    (value for value in sources.values() if value != "live"), current["source"]
                ),
            )
            return
        if current.get("candidate"):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "새 제안을 채택하거나 현재안을 유지한 뒤 진행하세요"
            )
        if action.startswith("revise:"):
            proposal_id = action.removeprefix("revise:")
            if any(item.id == proposal_id and item.required for item in proposals):
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "이전 승인 수정은 이 화면에서 바꿀 수 없습니다"
                )
            if proposal_id not in ids or not prompt.strip() or len(prompt) > 2000:
                raise DdakToolError(
                    ErrorCode.CONFIG_INVALID, "제안과 1~2000자의 수정 요청이 필요합니다"
                )
            current = self._save(
                run_id,
                current,
                state="reviewing",
                selected=selected,
                error=None,
                prompt=redact(prompt.strip()),
                reviewing=proposal_id,
            )
            self._launch(run_id, current, "revise")
        elif action == "finalize":
            p = self._prepared(run_id)
            self.combine(p.source, proposals, selected)  # 충돌·의존성은 요청 접수 전에 알려준다.
            current = self._save(run_id, current, state="finalizing", selected=selected, error=None)
            self._launch(run_id, current, "finalize")
        elif action == "save":
            self._save(run_id, current, selected=selected, error=None)
        else:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "지원하지 않는 검토 동작입니다")

    def _launch(self, run_id: str, current: dict, action: str) -> None:
        async def work() -> None:
            try:
                p = self._prepared(run_id)
                proposals = self.proposals(current)
                if action == "finalize":
                    patch = self.combine(p.source, proposals, current["selected"])
                    successor = await self.finalize(
                        p,
                        patch,
                        current,
                        lambda: self._current(run_id, current["revision"], busy=True),
                    )
                    self.service.store.publish_patch_review(run_id, current["revision"], successor)
                    return
                previous = next((p for p in proposals if p.id == current.get("reviewing")), None)
                context = replace(p.context, toggles={**p.context.toggles, "code_patch": True})
                result = await self.service._repository_work(
                    partial(
                        self.generate,
                        p.source,
                        context,
                        previous=previous,
                        allproposals=proposals if previous else None,
                        prompt=current.get("prompt", ""),
                    )
                )
                self._current(run_id, current["revision"], busy=True)
                warnings = list(
                    dict.fromkeys(
                        [*current.get("warnings", []), *(redact(w) for w in result.warnings)]
                    )
                )
                if previous is None:
                    self._save(
                        run_id,
                        current,
                        state="ready",
                        proposals=[p.model_dump(mode="json") for p in result.proposals],
                        selected=[p.id for p in result.proposals],
                        source=result.source.value,
                        proposal_sources={p.id: result.source.value for p in result.proposals},
                        error=None,
                        warnings=warnings,
                    )
                else:
                    if len(result.proposals) != 1 or result.proposals[0].id != previous.id:
                        raise DdakToolError(
                            ErrorCode.AI_OUTPUT_INVALID, "해당 제안 한 개의 수정 결과가 필요합니다"
                        )
                    proposal = result.proposals[0].model_copy(
                        update={"revision": previous.revision + 1}
                    )
                    revised = [proposal if item.id == proposal.id else item for item in proposals]
                    self.combine(p.source, revised, [item.id for item in revised])
                    candidate = {
                        "id": uuid.uuid4().hex,
                        "proposal": proposal.model_dump(mode="json"),
                        "source": result.source.value,
                        "base_revision": current["revision"],
                    }
                    self._save(
                        run_id,
                        current,
                        state="ready",
                        candidate=candidate,
                        error=None,
                        warnings=warnings,
                    )
            except asyncio.CancelledError:
                self._failed(
                    run_id, current, "서비스 종료로 요청이 중단됐습니다. 기존 제안을 유지했습니다."
                )
                raise
            except PatchLostError as exc:
                self.service.store.abort_patch_review_children(run_id, current["revision"])
                with contextlib.suppress(KeyError, DdakToolError):
                    self._save(
                        run_id,
                        current,
                        state="patch_lost",
                        candidate=None,
                        error=None,
                        loss_locations=list(exc.locations),
                    )
            except Exception as exc:
                detail = (
                    redact(exc.message)
                    if isinstance(exc, DdakToolError)
                    else "검토 요청에 실패했습니다. 기존 제안을 유지했습니다."
                )
                self._failed(run_id, current, detail)

        self.tasks[run_id] = asyncio.create_task(work(), name=f"patch-review-{run_id}")

    def _failed(self, run_id: str, current: dict, detail: str) -> None:
        self.service.store.abort_patch_review_children(run_id, current["revision"])
        # 승인 거절·새 소스·다른 revision을 이전 작업이 덮어쓰지 않는다.
        with contextlib.suppress(KeyError, DdakToolError):
            self._save(run_id, current, state="ready", error=detail[:1000])

    def view(self, run_id: str) -> dict | None:
        data = self.get(run_id)
        if data is None:
            return None
        data = {**data, "busy": data["state"] in BUSY}
        p = self._prepared(run_id)
        if data["busy"] or data["state"] == "patch_lost":
            return data
        proposals = self.proposals(data)
        data["items"] = [
            {
                **item.model_dump(mode="json"),
                "diff": redact(self.diff(p.source, item, proposals), max_len=None),
            }
            for item in proposals
        ]
        if data.get("candidate"):
            candidate = CodeProposal.model_validate(data["candidate"]["proposal"])
            data["candidate"] = {
                **data["candidate"],
                "diff": redact(
                    self.diff(
                        p.source,
                        candidate,
                        [candidate if item.id == candidate.id else item for item in proposals],
                    ),
                    max_len=None,
                ),
            }
        return data

    async def shutdown(self) -> None:
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

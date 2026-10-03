"""시연 초기화의 공개 상태. PR merge와 실제 서비스 복귀를 별도로 판정한다."""

from __future__ import annotations

import json
import threading
import time

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.demo_backend import DemoBackend, probe_version
from ddak.core.demo_cycle import ACTIONS
from ddak.core.redact import redact

STAGES = ("PR 열림", "merge됨", "배포 준비", "승인 대기", "v1 배포 완료")


def selected_targets(saved):
    target = saved.get("default_targets", "onprem")
    return ["local", "cloud"] if target == "both" else ["local" if target == "onprem" else target]


class DemoReset:
    def __init__(self, deployment, *, backend=None, probe=probe_version, urls=None):
        self.deployment = deployment
        self.root = deployment.root / "demo-reset"
        self.backend = backend or DemoBackend(deployment.root)
        self.probe = probe
        self.urls = urls or (lambda project, saved: {})
        self._lock = threading.Lock()

    def _path(self, project):
        # HTTP 밖의 직접 호출도 프로젝트 경로를 제한한다.
        import re

        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "프로젝트 이름 형식 오류")
        return self.root / (project + ".json")

    def _records(self, project):
        path = self._path(project)
        return json.loads(path.read_text()) if path.exists() else {}

    def create(self, project, action):
        if action not in ACTIONS:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "지원하지 않는 시연 요청")
        saved = self.deployment.get_project_settings(project) or {}
        if saved.get("watch_branch", "prod") != "prod" or not saved.get("auto_detect"):
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "프로젝트 설정에서 prod 자동 감시를 켜세요"
            )
        if not self._lock.acquire(blocking=False):
            raise DdakToolError(ErrorCode.LOCK_HELD, "시연 PR 준비 중입니다. 잠시 후 확인하세요")
        try:
            record = self.backend.create(project, saved, action)
            record.update(targets=selected_targets(saved), created_at=time.time())
            records = self._records(project)
            records[action] = record
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(project)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(records, ensure_ascii=False))
            tmp.replace(path)
            return record
        finally:
            self._lock.release()

    def view(self, project):
        saved = self.deployment.get_project_settings(project) or {}
        records = self._records(project)
        environments = self.deployment.get_environments(project)
        releases = {env: row.get("current") or {} for env, row in environments.items()}
        sources = {r.get("source_sha") for r in releases.values() if r.get("source_sha")}
        remote, warning = {"prs": {}, "labels": {}}, None
        if saved.get("repo_url"):
            try:
                if any(r.get("repo_url") != saved["repo_url"] for r in records.values()):
                    raise DdakToolError(
                        ErrorCode.PRECONDITION_FAILED,
                        "저장소 설정이 바뀌었습니다. 시연 PR을 다시 준비하세요",
                    )
                with self._lock:
                    remote = self.backend.inspect(project, saved, records, sources)
            except Exception as error:
                warning = (
                    redact(error.message)
                    if isinstance(error, DdakToolError)
                    else "시연 상태 조회 실패"
                )
        urls = self.urls(project, saved)
        observed = {}
        for env in selected_targets(saved):
            release = releases.get(env, {})
            run_id = release.get("release_id")
            context = self.deployment.get_run(run_id).get("context", {}) if run_id else {}
            url = urls.get(env)
            if not url:
                url = (
                    context.get("public_url")
                    if env == "local"
                    else (
                        "https://" + context["cloud_domain"]
                        if context.get("cloud_domain")
                        else None
                    )
                )
            value = {
                "environment": env,
                "label": "온프레미스" if env == "local" else "클라우드",
                "url": url,
                "version": "확인 불가",
                "sha": "",
                "confirmed": False,
                "detail": "공개 서비스 주소 등록 필요",
                "release_id": None,
            }
            if url:
                try:
                    live = self.probe(url)
                    value.update(
                        release_id=live.get("release_id"),
                        response=live,
                        detail="/version 응답 확인",
                    )
                    matched = bool(run_id and live.get("release_id") == run_id)
                    if matched:
                        value.update(
                            sha=release.get("source_sha", ""),
                            version=remote["labels"].get(release.get("source_sha"), "확인 불가"),
                            confirmed=environments[env]["status"] == "SUCCEEDED",
                        )
                    else:
                        value.update(
                            sha=live.get("source_sha", ""),
                            version=live.get("version")
                            if live.get("version") in ("v1", "v2", "v3")
                            else "확인 불가",
                            detail="/version 응답과 제품 성공 기록 일치 미확인",
                        )
                except Exception:
                    value["detail"] = "/version 연결·응답 확인 실패"
            observed[env] = value
        actions = []
        for name, record in records.items():
            pr = remote["prs"].get(name, {})
            sha = pr.get("merge_sha")
            run = None
            if sha:
                for row in self.deployment.list_runs(limit=200):
                    if row["project"] != project:
                        continue
                    item = self.deployment.get_run(row["run_id"])
                    context = item.get("context", {})
                    if (
                        context.get("source_sha") == sha
                        and (context.get("ref") or "").removeprefix("refs/heads/") == "prod"
                    ):
                        run = item
                        break
            stage = "PR 열림" if record.get("number") else "merge됨"
            if pr.get("closed") and not pr.get("merged"):
                stage = "PR 닫힘(merge 안 됨)"
            elif pr.get("merged"):
                stage = "merge됨"
                if run:
                    status = run["status"]
                    stage = "승인 대기" if status == "AWAITING_APPROVAL" else "배포 준비"
                    if status == "RUNNING":
                        stage = "배포 진행 중"
                    elif status == "SUCCEEDED":
                        stage = "배포 기록 성공 · 실제 서비스 확인 중"
                    elif status not in ("AWAITING_APPROVAL", "RUNNING"):
                        stage = "배포 확인 필요 · " + status
                elif self.deployment.list_preparations(project):
                    stage = "배포 준비"
                expected = record["tag"]
                if all(
                    observed.get(env, {}).get("confirmed")
                    and observed[env]["version"] == expected
                    and observed[env]["sha"] == sha
                    for env in record["targets"]
                ):
                    stage = expected + " 배포 완료"
            actions.append(
                {
                    **record,
                    "action": name,
                    "stage": stage,
                    "run_id": run["run_id"] if run else None,
                    "run_status": run["status"] if run else None,
                }
            )
        return {
            "environments": list(observed.values()),
            "actions": actions,
            "stages": STAGES,
            "warning": warning,
            "targets": selected_targets(saved),
            "available_tags": remote.get("available_tags", []),
        }

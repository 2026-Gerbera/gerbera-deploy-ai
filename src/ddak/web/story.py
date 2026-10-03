"""배포 기록을 화면에서 읽는 문장과 표로 바꾼다."""

from __future__ import annotations

import re

from ddak.core.redact import redact
from ddak.web.narrative import ENV, SOURCES, explain, pipeline_view, planned_rows, wording


def step_title(step: str) -> str:
    return wording(step)["name"]


def digest(ref: str | None) -> str:
    match = re.search(r"sha256:([a-f0-9]{64})", ref or "")
    return match[1][:12] if match else "기록 없음"


def recorded_version(*records: dict) -> str:
    for record in records:
        for key in ("version", "ref"):
            value = record.get(key)
            if isinstance(value, str):
                value = value.removeprefix("refs/tags/")
                if re.fullmatch(r"v[0-9]+(?:\.[0-9]+)*", value):
                    return value
    return "버전 기록 없음"


def steps(plan: dict) -> list[dict]:
    return [
        s
        for section in [
            plan.get("build", {}),
            *plan.get("deploy", {}).values(),
            plan.get("verify", {}),
        ]
        for s in section.get("steps", [])
    ]


def approval_story(view: dict, data: dict | None = None) -> dict:
    data = data or {}
    plan = view.get("plan") or {}
    entries = steps(plan)
    targets = [
        env for env in ("local", "cloud") if plan.get("deploy", {}).get(env, {}).get("steps")
    ]
    builds = sorted(
        {s.get("tier") or s["id"].split(".")[1] for s in entries if s.get("tool") == "build_image"}
    )
    deployed = sorted({s.get("tier") for s in entries if s.get("tier")})
    images = [
        {"tier": tier.upper(), "action": "새로 빌드" if tier in builds else "이전 이미지 재사용"}
        for tier in sorted(set(deployed + builds))
    ]
    ref = view.get("ref") or "배포 소스"
    sha = (view.get("source_sha") or "")[:7] or "미확인"
    build_text = (
        f"{'·'.join(t.upper() for t in builds)} 이미지를 새로 빌드합니다."
        if builds
        else "새 이미지 빌드 단계가 없습니다."
    )
    return {
        # 표시 자료가 없어도 템플릿이 같은 키를 읽도록 빈 목록을 기본으로 둔다.
        "files": [],
        "patches": [],
        "findings": [],
        "mappings": [],
        **data,
        "summary": (
            f"{ref}의 커밋 {sha}를 "
            f"{'·'.join(ENV[t] for t in targets) or '계획된 환경'}에 배포합니다. "
            f"{build_text}"
        ),
        "targets": targets,
        "images": images,
        "containers": [
            {"env": ENV.get(s.get("target"), "공통"), "tier": (s.get("tier") or "앱").upper()}
            for s in entries
            if s.get("tool") == "deploy_tier"
        ],
        "migration": any(
            s.get("tool") in {"run_migrations", "migrate", "run_migration"}
            or ".migrate." in s["id"]
            for s in entries
        ),
        "steps": [{**s, "title": step_title(s["id"])} for s in entries],
        "plan_rows": planned_rows(
            plan, view.get("build_backend"), (view.get("infra_summary") or {}).get("storage")
        ),
        "infra": infra_story(view.get("infra_summary") or {}),
    }


def result_story(run: dict, release: dict | None, data: dict) -> dict:
    release = release or {}
    context = run.get("context") or {}
    result = run.get("result") or {}
    records = result.get("steps") or {}
    previous = data.get("previous", {})
    rows = []
    for env in ("local", "cloud"):
        track = result.get("tracks", {}).get(env, "UNKNOWN")
        deployed = release.get("environment_images", {}).get(env, {})
        images = deployed.get("images", {})
        if not images and track == "DONE":
            images = release.get("images", {})
        old = {
            **context.get("previous_release", {}).get(env, {}),
            **previous.get(env, {}),
        }
        for tier in sorted(set(images) | set(old.get("images", {}))):
            rows.append(
                {
                    "env": env,
                    "tier": tier.upper(),
                    "before": digest(old.get("images", {}).get(tier)),
                    "after": digest(images.get(tier))
                    if track == "DONE"
                    else "이전 버전 복구"
                    if track == "ROLLED_BACK"
                    else "적용 미확인",
                    "source_before": (old.get("source_sha") or "")[:7] or "첫 배포 / 기록 없음",
                    "source_after": (
                        deployed.get("source_sha")
                        or release.get("source_sha")
                        or context.get("source_sha")
                        or ""
                    )[:7]
                    or "기록 없음",
                    "version_before": recorded_version(old),
                    "version_after": recorded_version(deployed, release, context),
                }
            )
    checks = []
    scenarios = []
    for sid, record in records.items():
        if not sid.startswith("verify.") or sid in {
            "verify.report",
            "verify.watch",
            "verify.compare",
            "verify.diagnose",
        }:
            continue
        output = record.get("output") or {}
        observed = [item for item in output.get("scenarios", []) if isinstance(item, dict)]
        scenarios.extend(observed)
        env = "local" if sid.endswith(".local") else "cloud" if sid.endswith(".cloud") else "common"
        checks.append(
            {
                "id": sid,
                "env": env,
                "title": step_title(sid),
                "status": record.get("status"),
                "elapsed_s": record.get("elapsed_s"),
                "scenarios": {
                    "passed": sum(item.get("ok") is True for item in observed),
                    "total": len(observed),
                }
                if "scenarios" in output
                else None,
                "sentence": "검증 기록을 확인했습니다."
                if record.get("status") == "succeeded"
                else "검증 기준을 통과하지 못했습니다.",
            }
        )
    failed = next(
        (
            (sid, records[sid])
            for sid in [*data.get("failed_steps", []), *records]
            if sid in records and records[sid].get("status") in {"failed", "check_failed"}
        ),
        None,
    )
    failure = None
    if failed:
        sid, record = failed
        reason = (
            "서비스 응답이 기준을 통과하지 못했습니다."
            if "health" in sid
            else "사용자 시나리오 검증이 실패했습니다."
            if "smoke" in sid
            else "이미지를 만들지 못했습니다."
            if sid.startswith("build.")
            else "이 단계의 실행을 완료하지 못했습니다."
        )
        failure = {
            "stage": step_title(sid),
            "reason": reason,
            "next": "연결 설정과 대상 환경 상태를 확인한 뒤 새 배포를 준비하세요.",
            "detail": redact(record.get("error") or "상세 원인 기록 없음"),
        }
    elapsed = (
        run.get("finished", 0) - run.get("created", 0)
        if run.get("finished") and run.get("created")
        else None
    )
    return {
        "version": recorded_version(release, context),
        "metrics": {
            "new": sum(
                item["action"] == "새로 빌드"
                for item in approval_story({"plan": data.get("plan") or {}})["images"]
            ),
            "reuse": sum(
                item["action"] != "새로 빌드"
                for item in approval_story({"plan": data.get("plan") or {}})["images"]
            ),
            "environments": sum(
                track not in {"N/A", "SKIPPED", "UNKNOWN"}
                for name, track in result.get("tracks", {}).items()
                if name in {"local", "cloud"}
            ),
            "passed": sum(scenario.get("ok") is True for scenario in scenarios),
            "checks": len(scenarios),
        },
        "images": rows,
        "checks": checks,
        "failure": failure,
        "elapsed_s": elapsed,
        "commit": (release.get("source_sha") or context.get("source_sha") or "")[:7] or "기록 없음",
        "build_backend": {"local": "온프레미스 로컬 빌드", "codebuild": "클라우드 CodeBuild"}.get(
            context.get("build_backend"), "빌드 위치 기록 없음"
        ),
        "build_images": approval_story({"plan": data.get("plan") or {}})["images"],
        "compare": comparison(records),
        "diagnose": diagnosis(records, run.get("status")),
        "execution": pipeline_view(
            run,
            data.get("plan") or {},
            data.get("events") or [],
            storage=(data.get("infra_summary") or {}).get("storage"),
        )["rows"],
    }


def comparison(records: dict) -> dict:
    record = records.get("verify.compare", {})
    output = record.get("output") or {}
    checks = output.get("checks") or []
    return {
        "available": bool(record),
        "skipped": record.get("status") == "skipped",
        "source": SOURCES.get(output.get("source"), ""),
        "counts": {
            kind: sum(row.get("verdict") == kind for row in checks)
            for kind in ("match", "expected_diff", "mismatch")
        },
        "differences": [
            {
                "name": explain(str(row.get("id", "")).split(".")[0], "scenario"),
                "sentence": "두 환경에서 같은 결과를 내지 못했습니다.",
            }
            for row in checks
            if row.get("verdict") == "mismatch"
        ],
    }


def diagnosis(records: dict, status: str | None = None) -> dict:
    record = records.get("verify.diagnose", {})
    output = record.get("output") or {}
    skipped = record.get("status") == "skipped"
    return {
        "available": bool(record),
        "skipped": skipped,
        "category": "실패가 없어 생략"
        if skipped and status == "SUCCEEDED"
        else "이번 실행에서는 생략"
        if skipped
        else explain(output.get("category"), "category"),
        "next": "해당 환경의 연결 설정과 검증 결과를 확인하고 새 배포를 준비하세요.",
        "source": SOURCES.get(output.get("source"), ""),
        "hypothesis": not skipped,
    }


def infra_story(infra: dict) -> dict:
    headline = str(infra.get("headline") or "")
    platform = re.search(r"클라우드 플랫폼 ([A-Za-z0-9_-]+)", headline)
    source = re.search(r"HCL source=(live|cache|replay|fixture)", headline)
    role_names = {
        "codebuild": "CodeBuild 역할",
        "dbinit": "DB 초기화 역할",
        "task_execution": "태스크 실행 역할",
        "execution": "태스크 실행 역할",
        "task": "앱 태스크 역할",
    }
    services = {
        "logs": "로그",
        "secretsmanager": "시크릿",
        "s3": "S3",
        "ecr": "이미지 저장소",
        "ecs": "컨테이너",
        "rds": "데이터베이스",
        "kms": "암호화 키",
        "iam": "권한 관리",
    }
    roles, boundaries = [], []
    for item in infra.get("iam_diff") or []:
        if not isinstance(item, dict):
            continue
        address = str(item.get("address") or "")
        boundaries.extend(item.get("boundary_changes") or [])
        if address == "ddak.foundation":
            continue
        title = next((name for key, name in role_names.items() if key in address), "배포 역할")
        grouped = {}
        grants = item.get("proposed_allow", item.get("added"))
        for grant in grants if isinstance(grants, list) else []:
            if not isinstance(grant, dict):
                continue
            resources = grant.get("resources") if isinstance(grant.get("resources"), list) else []
            for action in grant.get("actions") if isinstance(grant.get("actions"), list) else []:
                if not isinstance(action, str) or ":" not in action:
                    continue
                service, operation = action.split(":", 1)
                row = grouped.setdefault(
                    service,
                    {
                        "name": services.get(service, "서비스 권한"),
                        "rights": set(),
                        "count": len(resources),
                    },
                )
                row["rights"].add(
                    "읽기"
                    if operation.startswith(("Get", "List", "Describe", "BatchGet"))
                    else "쓰기"
                    if operation.startswith(("Put", "Create", "Upload", "Start", "Update"))
                    else "관리"
                )
        roles.append(
            {
                "title": title,
                "status": "변경 없음" if item.get("action") == "unchanged" else "변경",
                "boundary": item.get("boundary_attached") is True,
                "services": [
                    {
                        "name": row["name"],
                        "rights": "·".join(sorted(row["rights"])),
                        "scope": f"{row['count']}개",
                    }
                    for row in grouped.values()
                ],
                "raw": item,
            }
        )
    return {
        "available": bool(infra),
        "counts": {
            key: (infra.get("counts") or {}).get(key)
            for key in ("create", "update", "delete", "replace")
        },
        "platform": platform[1] if platform else "기록 없음",
        "source": {
            "live": "AI 생성",
            "cache": "기준본",
            "replay": "저장 응답",
            "fixture": "검증용 데이터",
        }.get(source[1] if source else infra.get("source"), "기록 없음"),
        "foundation": "state 버킷·권한 경계 확인"
        if "기반" in headline
        or any(
            item.get("address") == "ddak.foundation"
            for item in infra.get("iam_diff") or []
            if isinstance(item, dict)
        )
        else "기록 없음",
        "http_exception": "ALB 80→443 리다이렉트" if "공개 HTTP 예외" in headline else "",
        "roles": roles,
        "boundaries": boundaries,
        "boundary_changed": any(item.get("action") in {"create", "update"} for item in boundaries),
        "storage": infra.get("storage"),
        "storage_text": wording("deploy.infra.cloud", storage=infra.get("storage")),
    }


def environment_cards(run: dict, story: dict, links: list[dict]) -> list[dict]:
    result = run.get("result") or {}
    cards = []
    targets = (run.get("context") or {}).get("targets")
    for env in ("local", "cloud"):
        excluded = (env == "local" and targets == "cloud") or (
            env == "cloud" and targets in {"onprem", "local"}
        )
        status = result.get("tracks", {}).get(env, "N/A" if excluded else "RUNNING")
        rows = [row for row in story["execution"] if row["track"] == env]
        active = next((row for row in rows if row["status"] == "running"), None)
        images = [row for row in story["images"] if row["env"] == env]
        tiers = {row["tier"].lower() for row in images}
        tiers.update(row["id"].split(".")[1] for row in rows if row.get("tool") == "deploy_tier")
        shared_builds = [
            row
            for row in story["execution"]
            if row["track"] == "common"
            and (row.get("tool") == "build_image" or row["id"].startswith("build."))
            and row["id"].split(".")[1] in tiers
            and status not in {"N/A", "SKIPPED"}
        ]
        execution = []
        checks = [row for row in story["checks"] if row["env"] == env]
        check_ids = {row["id"] for row in checks}
        for row in [*shared_builds, *rows]:
            if row["id"] in check_ids:
                continue
            item = dict(row)
            if row.get("tool") == "build_image" or row["id"].startswith("build."):
                item["build_action"] = (
                    "재사용"
                    if row["status"] == "skipped" and "재사용" in row.get("why", "")
                    else "빌드 생략"
                    if row["status"] == "skipped"
                    else "새로 빌드"
                )
            execution.append(item)
        cards.append(
            {
                "env": env,
                "status": status,
                "busy": status in {"RUNNING", "WAITING", "PENDING"},
                "elapsed_s": result.get("track_elapsed_s", {}).get(
                    env, sum(row.get("elapsed_s") or 0 for row in rows) if rows else None
                ),
                "current": active["name"] if active else "다음 작업 준비",
                "images": images,
                "checks": checks,
                "execution": execution,
                "link": next((link for link in links if link["label"] == ENV[env]), None)
                if status == "DONE"
                else None,
            }
        )
    return cards

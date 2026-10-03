"""배포 기록을 화면에서 읽는 문장과 표로 바꾼다."""

from __future__ import annotations

import re

from ddak.core.redact import redact

ENV = {"local": "온프레미스", "cloud": "클라우드"}
STEP = {
    "build.was": "WAS 이미지 빌드",
    "build.web": "WEB 이미지 빌드",
    "build.db": "DB 이미지 준비",
    "deploy.infra": "클라우드 리소스 적용",
    "deploy.was": "WAS 컨테이너 교체",
    "deploy.web": "WEB 컨테이너 교체",
    "deploy.app": "앱 컨테이너 교체",
    "deploy.db": "DB 컨테이너 준비",
    "deploy.config": "환경 설정 주입",
    "deploy.secrets": "비밀값 동기화",
    "deploy.migrate": "DB 마이그레이션",
    "deploy.dbinit": "DB 초기화",
    "deploy.tls": "HTTPS 연결 준비",
    "verify.health": "서비스 응답 확인",
    "verify.smoke": "사용자 시나리오 확인",
    "verify.compare": "두 환경 동작 비교",
    "verify.tls": "HTTPS 확인",
    "verify.report": "결과 저장",
    "verify.watch": "배포 후 상태 관찰",
}


def step_title(step: str) -> str:
    base = re.sub(r"\.(local|cloud)$", "", step)
    return STEP.get(base, "실행 작업")


def digest(ref: str | None) -> str:
    match = re.search(r"sha256:([a-f0-9]{64})", ref or "")
    return match[1][:12] if match else "기록 없음"


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
    targets = [env for env in ENV if plan.get("deploy", {}).get(env, {}).get("steps")]
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
    }


def result_story(run: dict, release: dict | None, data: dict) -> dict:
    release = release or {}
    context = run.get("context") or {}
    result = run.get("result") or {}
    records = result.get("steps") or {}
    previous = data.get("previous", {})
    rows = []
    for env in ENV:
        track = result.get("tracks", {}).get(env, "UNKNOWN")
        images = release.get("environment_images", {}).get(env, {}).get("images", {})
        if not images and track == "DONE":
            images = release.get("images", {})
        old = previous.get(env, {})
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
                    "source_after": (release.get("source_sha") or context.get("source_sha") or "")[
                        :7
                    ]
                    or "기록 없음",
                }
            )
    checks = []
    for sid, record in records.items():
        if not sid.startswith("verify.") or sid in {"verify.report", "verify.watch"}:
            continue
        output = record.get("output") or {}
        env = "local" if sid.endswith(".local") else "cloud" if sid.endswith(".cloud") else "common"
        checks.append(
            {
                "env": env,
                "title": step_title(sid),
                "status": record.get("status"),
                "elapsed_s": record.get("elapsed_s"),
            }
        )
        for scenario in output.get("scenarios", []):
            checks.append(
                {
                    "env": env,
                    "title": scenario.get("id", "시나리오"),
                    "status": "succeeded" if scenario.get("ok") is True else "check_failed",
                    "elapsed_s": None,
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
        "images": rows,
        "checks": checks,
        "failure": failure,
        "elapsed_s": elapsed,
        "commit": (release.get("source_sha") or context.get("source_sha") or "")[:7] or "기록 없음",
    }

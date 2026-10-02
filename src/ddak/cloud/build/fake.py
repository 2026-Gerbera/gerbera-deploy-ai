"""가짜 CodeBuild 클라이언트(테스트·드라이런·UI 개발). AWS·Docker Hub를 건드리지 않는다.

진짜 boto3 클라이언트 자리에 넣어 codebuild.run_build·release.build_release를 그대로 통과시킨다.
S3 소스에는 실제 CodeBuild처럼 resolvedSourceVersion을 채우지 않는다(sourceVersion만).
digest는 (tier, 커밋 SHA, 플랫폼)으로 정해지는 결정적 값이다. 같은 커밋이면 같은 digest.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ddak.cloud.build.codebuild import ENV_REVISION, ENV_TIERS, exported_names


def fake_digest(*parts: str) -> str:
    return "sha256:" + hashlib.sha256("\0".join(parts).encode()).hexdigest()


@dataclass
class FakeCodeBuild:
    """start_build를 받으면 바로 성공한 빌드를 만든다. fail_status를 주면 그 상태로 끝난다."""

    fail_status: str | None = None
    started: list[dict[str, Any]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    _builds: dict[str, dict[str, Any]] = field(default_factory=dict)

    def start_build(self, **kwargs: Any) -> dict[str, Any]:
        self.started.append(kwargs)
        build_id = f"{kwargs['projectName']}:fake-{len(self.started)}"
        env = {v["name"]: v["value"] for v in kwargs.get("environmentVariablesOverride", [])}
        revision = env[ENV_REVISION]
        exported: list[dict[str, str]] = []
        for tier in env[ENV_TIERS].split(","):
            index_name, platform_names = exported_names(tier)
            exported.append({"name": index_name, "value": fake_digest(tier, revision, "index")})
            exported += [
                {"name": name, "value": fake_digest(tier, revision, platform)}
                for platform, name in platform_names.items()
            ]
        # 실제 프로젝트의 기본 buildspec은 실패 전용이다. override 없이 시작하면 실패한다
        status = self.fail_status or ("SUCCEEDED" if kwargs.get("buildspecOverride") else "FAILED")
        build: dict[str, Any] = {
            "id": build_id,
            "buildStatus": status,
            "sourceVersion": kwargs["sourceVersion"],
            "exportedEnvironmentVariables": exported,
        }
        # 실제 CodeBuild처럼 S3 소스에는 resolvedSourceVersion을 채우지 않는다
        if kwargs.get("sourceTypeOverride") != "S3":
            build["resolvedSourceVersion"] = kwargs["sourceVersion"]
        self._builds[build_id] = build
        return {"build": {"id": build_id}}

    def batch_get_builds(self, **kwargs: Any) -> dict[str, Any]:
        return {"builds": [self._builds[i] for i in kwargs["ids"] if i in self._builds]}

    def stop_build(self, **kwargs: Any) -> dict[str, Any]:
        self.stopped.append(kwargs["id"])
        return {"build": {"id": kwargs["id"], "buildStatus": "STOPPED"}}


__all__ = ["FakeCodeBuild", "fake_digest"]

"""C3 클라우드 검증의 안전한 로컬 경로."""

from __future__ import annotations

import pytest

import ddak.cloud.health.health as health
import ddak.cloud.health.tls as tls
from ddak.cloud.health import health_check
from ddak.cloud.health.fake import fake_verify_tls
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.release import ImageArtifact, ReleaseArtifacts, SnapshotBinding

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64


def _artifact(name: str, index: str, amd: str, arm: str) -> ImageArtifact:
    return ImageArtifact(
        ref=f"example/{name}@sha256:{index}",
        index_digest=f"sha256:{index}",
        platform_digests={"linux/amd64": f"sha256:{amd}", "linux/arm64": f"sha256:{arm}"},
    )


@pytest.mark.parametrize("domain", [None, "localhost", "https://app.example.com", "bad domain"])
def test_health_requires_valid_cloud_domain_before_aws_call(domain: str | None) -> None:
    with pytest.raises(DdakToolError, match="클라우드 도메인"):
        health_check(RunContext("run-1", cloud_domain=domain))


def test_expected_digests_include_current_and_carried_artifacts() -> None:
    snapshot = SnapshotBinding(
        source_snapshot_hash=f"sha256:{_A}", build_snapshot_hash=f"sha256:{_A}"
    )
    current = _artifact("was", _A, _B, _C)
    carried = _artifact("web", _B, _C, _D)
    ctx = RunContext(
        "run-1",
        release_artifacts=ReleaseArtifacts(snapshot=snapshot, images={"was": current}),
        previous_release={
            "cloud": {
                "artifacts": {
                    "snapshot": snapshot.model_dump(),
                    "images": {"web": carried.model_dump()},
                }
            }
        },
    )

    assert health._expected_image_digests(ctx) == {
        current.index_digest,
        *current.platform_digests.values(),
        carried.index_digest,
        *carried.platform_digests.values(),
    }


def test_health_uses_active_revision_and_retries_transient_alb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _artifact("was", _A, _B, _C)
    snapshot = SnapshotBinding(
        source_snapshot_hash=f"sha256:{_A}", build_snapshot_hash=f"sha256:{_A}"
    )
    ctx = RunContext(
        "run-1",
        cloud_domain="app.example.com",
        platform={
            "cloud": {
                "region": "ap-northeast-2",
                "cluster_name": "cluster",
                "ecs_service_name": "service",
                "target_group_arn": "target-group",
            }
        },
        release_artifacts=ReleaseArtifacts(snapshot=snapshot, images={"was": artifact}),
    )

    class Ecs:
        @staticmethod
        def describe_services(**kwargs):
            return {"services": [{"taskDefinition": "task:new"}]}

        @staticmethod
        def list_tasks(**kwargs):
            return {"taskArns": ["old", "new"]}

        @staticmethod
        def describe_tasks(**kwargs):
            return {
                "tasks": [
                    {
                        "taskDefinitionArn": "task:old",
                        "containers": [{"imageDigest": f"sha256:{_D}"}],
                    },
                    {
                        "taskDefinitionArn": "task:new",
                        "containers": [{"imageDigest": f"sha256:{_B}"}],
                    },
                ]
            }

    class Elb:
        calls = 0

        def describe_target_health(self, **kwargs):
            self.calls += 1
            state = "initial" if self.calls == 1 else "healthy"
            return {"TargetHealthDescriptions": [{"TargetHealth": {"State": state}}]}

    ecs, elb = Ecs(), Elb()
    monkeypatch.setattr(health, "client", lambda service, *_: ecs if service == "ecs" else elb)
    monkeypatch.setattr(
        health,
        "_json_get",
        lambda _domain, path, _timeout: (
            {"status": "ok"} if path == "/health/ready" else {"release_id": "run-1"}
        ),
    )
    monkeypatch.setattr(health.time, "sleep", lambda _seconds: None)

    result = health.health_check(ctx)

    assert result.passed is True
    assert elb.calls == 2


def test_fake_tls_is_deterministic_and_complete() -> None:
    result = fake_verify_tls("run-1")
    assert result.passed is True
    assert [check.id for check in result.checks] == [f"V{i}" for i in range(1, 10)]


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("https://app.example.com/", True),
        ("https://app.example.com:443/", True),
        ("https://app.example.com:444/", False),
        ("https://other.example.com/", False),
    ],
)
def test_http_redirect_accepts_only_same_host_https_default_port(
    monkeypatch: pytest.MonkeyPatch, location: str, expected: bool
) -> None:
    class HttpResponse:
        status = 301

        @staticmethod
        def getheader(name: str) -> str | None:
            return location if name == "Location" else None

        @staticmethod
        def read(limit: int) -> bytes:
            del limit
            return b""

    class Connection:
        def request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        @staticmethod
        def getresponse() -> HttpResponse:
            return HttpResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr(tls.http.client, "HTTPConnection", lambda *args, **kwargs: Connection())

    passed, _ = tls._http_redirect("app.example.com", 1)

    assert passed is expected

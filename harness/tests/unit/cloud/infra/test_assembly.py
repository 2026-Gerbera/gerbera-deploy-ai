from dataclasses import replace

import pytest

from ddak.cloud.infra.assembly import read_bundle
from ddak.core.contracts.errors import DdakToolError
from ddak.core.contracts.tools.generate_infra import GenerateInfraOutput
from ddak.core.snapshots import digest_bytes
from tests.unit.cloud.infra.test_runtime import SETTINGS


@pytest.mark.parametrize("bad", ["hash", "extra", "link", "directory"])
def test_generated_bundle_rejects_unbound_bytes(tmp_path, bad):
    data = b'resource "aws_secretsmanager_secret" "x" {}'
    p = tmp_path / "app.tf"
    p.write_bytes(data)
    bundle = GenerateInfraOutput(
        directory=str(tmp_path), layer="app", files={"app.tf": digest_bytes(data)}
    )
    assert read_bundle(bundle, tmp_path) == {"app.tf": data.decode()}
    if bad == "hash":
        p.write_bytes(b"changed")
    elif bad == "extra":
        (tmp_path / "hidden.tf").write_bytes(data)
    elif bad == "link":
        p.unlink()
        p.symlink_to(tmp_path.parent / "unused")
    else:
        bundle = bundle.model_copy(update={"directory": str(tmp_path.parent)})
    with pytest.raises(DdakToolError):
        read_bundle(bundle, tmp_path)


def test_image_repository_is_code_checked_literal_output():
    settings = replace(
        SETTINGS, layer="platform", outputs={"image_repository": ('"fixture/app"', "string")}
    )
    assert settings.framework()["output"]["image_repository"] == {"value": "fixture/app"}
    with pytest.raises(DdakToolError):
        replace(settings, outputs={"image_repository": ('"${file("secret")}"', "string")})


def test_plan_tool_bounds_summary_after_generation_source(monkeypatch):
    from types import SimpleNamespace

    from ddak.cloud.infra import bindings
    from ddak.core.config import AdapterMode
    from ddak.core.contracts.context import RunContext
    from ddak.core.contracts.enums import Source
    from ddak.core.contracts.errors import ErrorCode
    from ddak.core.contracts.tools.plan_infra import PlanInfraInput

    binding = SimpleNamespace(
        runtime=SimpleNamespace(plan=lambda **kw: {"headline": "x" * 8170}),
        read_session=lambda: None,
        analyzer=lambda: None,
        generation_source=Source.FIXTURE,
        mode=AdapterMode.FAKE,
    )
    monkeypatch.setattr(bindings, "_binding", lambda *a: binding)
    with pytest.raises(DdakToolError, match="8KiB") as exc:
        bindings.run_plan(PlanInfraInput(run_id="size-run"), RunContext("size-run"))
    assert exc.value.code is ErrorCode.CONFIG_INVALID

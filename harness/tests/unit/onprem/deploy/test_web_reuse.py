"""실제 build 헬퍼의 재사용 분기. 외부 Docker/push만 대역으로 바꾼다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ddak.core.contracts.release import ImageArtifact
from ddak.core.snapshots import digest_bytes
from tests.support import load_script

cli = load_script("o1_onprem")


@pytest.mark.parametrize("changed_web_file", ["flaskr/static/style.css", "docker/nginx/env.sh"])
def test_reuse_skips_web_build_until_its_inputs_change(tmp_path, monkeypatch, changed_web_file):
    source = tmp_path / "source"
    for filename in ("flaskr/blog.py", "flaskr/static/style.css", "docker/nginx/env.sh"):
        path = source / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("v1\n")
    (source / "images.lock.json").write_text((cli.APP / "images.lock.json").read_text())
    calls: list[str] = []

    def docker(*args):
        tier = Path(args[args.index("--file") + 1]).stem
        calls.append(tier)
        index = digest_bytes(str(len(calls)).encode())
        Path(args[args.index("--metadata-file") + 1]).write_text(
            json.dumps({"containerimage.digest": index})
        )
        return ""

    def inspect(ref):
        index = ref.rsplit("@", 1)[1]
        return ImageArtifact.model_validate(
            {
                "ref": ref,
                "index_digest": index,
                "platform_digests": {"linux/amd64": index, "linux/arm64": index},
            }
        )

    monkeypatch.setattr(cli, "docker", docker)
    monkeypatch.setattr(cli, "inspect_artifact", inspect)
    first, second, third = (tmp_path / f"v{i}.json" for i in (1, 2, 3))
    cli.build(source, "example/flaskr", first, three=True)
    initial = json.loads(first.read_text())
    assert calls == ["was", "web"]

    # 애플리케이션 코드만 바뀌면 snapshot/WAS는 바뀌고 web의 전체 산출물은 유지한다.
    (source / "flaskr/blog.py").write_text("v2\n")
    cli.build(source, "example/flaskr", second, three=True, reuse_web=first)
    reused = json.loads(second.read_text())
    assert calls == ["was", "web", "was"]
    assert reused["artifacts"]["images"]["web"] == initial["artifacts"]["images"]["web"]
    assert reused["artifacts"]["images"]["was"] != initial["artifacts"]["images"]["was"]
    assert reused["artifacts"]["snapshot"] != initial["artifacts"]["snapshot"]
    assert reused["web_inputs"] == initial["web_inputs"]

    # --reuse-web을 줬어도 static/nginx 설정이 바뀌면 새 web 이미지를 만든다.
    (source / changed_web_file).write_text("v2\n")
    cli.build(source, "example/flaskr", third, three=True, reuse_web=second)
    rebuilt = json.loads(third.read_text())
    assert calls == ["was", "web", "was", "was", "web"]
    assert rebuilt["artifacts"]["images"]["web"] != reused["artifacts"]["images"]["web"]
    assert rebuilt["web_inputs"] != reused["web_inputs"]

"""source=fixture: 수정된 원본의 비밀값은 남기지 않되 제거 패치는 허용한다."""

import pytest

from ddak.core import candidate
from ddak.core.contracts.errors import DdakToolError
from ddak.core.patch_ledger import file_diff
from tests.unit.core.test_app_repository import git
from tests.unit.core.test_candidate import operator_identity, repository  # noqa: F401


def test_source_finding_requires_removal_before_approval(repository, monkeypatch):  # noqa: F811
    repo, _, _, _ = repository
    repo.secret_scan = None
    git(repo.path, "switch", "prod")
    old = b"SECRET_KEY = " + repr("dev").encode() + b"\n"
    fixed = b"import os\nSECRET_KEY = os.environ['SECRET_KEY']\n"
    (repo.path / "config.py").write_bytes(old)
    git(repo.path, "add", "config.py")
    git(repo.path, "commit", "-m", "Fixture development setting")
    sha = git(repo.path, "rev-parse", "HEAD")
    git(repo.path, "push", "origin", "prod")

    def scan(root):
        data = (root / "config.py").read_bytes()
        return {("config.py", "fixture-rule", 1)} if data == old else set()

    monkeypatch.setattr(candidate, "_scan_findings", scan)
    with pytest.raises(DdakToolError, match="비밀값"):
        candidate.preflight_source(repo, sha)
    checked = candidate.preflight_source(repo, sha, patch=file_diff("config.py", old, fixed))
    assert checked["ignored_count"] == 0
    monkeypatch.setattr(
        candidate, "_scan_findings", lambda root: {("config.py", "fixture-rule", 1)}
    )
    with pytest.raises(DdakToolError, match="비밀값"):
        candidate.preflight_source(repo, sha, patch=file_diff("config.py", old, fixed))

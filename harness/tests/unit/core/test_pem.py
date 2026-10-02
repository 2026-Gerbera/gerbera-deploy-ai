"""source=fixture: 공개 인증서만 허용하며 실제 개인키·외부 서비스는 사용하지 않는다."""

import base64
import difflib
from pathlib import Path

import pytest

from ddak.cloud.build.local import _manifest as local_manifest
from ddak.core.candidate import preflight_source, template_file, tree_manifest
from ddak.core.contracts.errors import DdakToolError
from ddak.core.pem import MAX_PEM_BYTES, UnsupportedPemError, is_certificate_only_pem
from ddak.core.snapshots import apply_diff, copy_source, excluded, file_manifest, preview
from tests.unit.core import test_candidate as candidate_support
from tests.unit.core.test_app_repository import git
from tests.unit.plan.intake import test_intake as intake

repository = candidate_support.repository
operator_identity = candidate_support.operator_identity

CERT = (Path(__file__).parents[2] / "fixtures/certificates/public-ca.pem.txt").read_bytes()
BUNDLE = b"# public CA fixture\n; comment\n" + CERT + b"\n" + CERT
NAME = "certs/global-bundle.pem"
PRIVATE = b"-----BEGIN PRIVATE KEY-----\nZmFrZQ==\n-----END PRIVATE KEY-----\n"
CSR = b"-----BEGIN CERTIFICATE REQUEST-----\nZmFrZQ==\n-----END CERTIFICATE REQUEST-----\n"
INVALID = [
    PRIVATE,
    CERT + PRIVATE,
    CSR,
    b"",
    b"\xff\x00binary",
    b" " * (MAX_PEM_BYTES + 1),
    CERT.replace(b"CERTIFICATE", b"ENCRYPTED PRIVATE KEY"),
    CERT.replace(b"CERTIFICATE", b"RSA PRIVATE KEY"),
    CERT.replace(b"CERTIFICATE", b"EC PRIVATE KEY"),
    CERT.replace(b"CERTIFICATE", b"OPENSSH PRIVATE KEY"),
    CERT + b"uncommented trailing text",
    CERT.replace(b"-----END CERTIFICATE-----", b""),
    b"-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n",
    b"#\x00binary comment\n" + CERT,
    b"# " + PRIVATE,
]


def test_public_bundle_and_byte_limit():
    assert is_certificate_only_pem(BUNDLE)
    assert is_certificate_only_pem(CERT * 111)
    assert is_certificate_only_pem("# 공개 인증서\n".encode() + CERT)
    assert is_certificate_only_pem(CERT + b" " * (MAX_PEM_BYTES - len(CERT)))
    assert not excluded(Path(NAME))


@pytest.mark.parametrize("data", INVALID, ids=[f"invalid-{i}" for i in range(len(INVALID))])
def test_unsupported_pem_rejected_everywhere_without_content(tmp_path, data):
    assert not is_certificate_only_pem(data)
    path = tmp_path / NAME
    path.parent.mkdir()
    path.write_bytes(data)
    for inspect in (file_manifest, local_manifest):
        with pytest.raises((UnsupportedPemError, DdakToolError)) as caught:
            inspect(tmp_path)
        assert NAME in str(caught.value) and "개인키/미지원 PEM" in str(caught.value)
        assert "BEGIN" not in str(caught.value) and "ZmFrZQ" not in str(caught.value)


@pytest.mark.parametrize("name", [NAME, "certs/ROOT.PEM"])
def test_certificates_survive_copy_and_local_build_manifest(tmp_path, name):
    source = tmp_path / "source"
    path = source / name
    path.parent.mkdir(parents=True)
    path.write_bytes(BUNDLE)
    target = tmp_path / "build"
    manifest = copy_source(source, target)
    assert set(manifest) == {name}
    assert (target / name).read_bytes() == BUNDLE
    assert local_manifest(source) == file_manifest(target) == manifest
    assert not template_file(Path(name), frozenset({name}))


@pytest.mark.parametrize("name", ["certs/file.key", "certs/file.KEY"])
def test_key_extension_rejected_even_for_public_certificate(tmp_path, name):
    path = tmp_path / name
    path.parent.mkdir()
    path.write_bytes(CERT)
    with pytest.raises(UnsupportedPemError, match="개인키/미지원 PEM") as error:
        file_manifest(tmp_path)
    assert name in str(error.value)


def test_encoded_trailing_data_is_not_a_certificate():
    der = base64.b64decode(b"".join(CERT.splitlines()[1:-1]))
    wrapped = (
        b"-----BEGIN CERTIFICATE-----\n"
        + base64.b64encode(der + b"other data")
        + b"\n-----END CERTIFICATE-----\n"
    )
    assert not is_certificate_only_pem(wrapped)


def diff(before, after):
    return "".join(
        difflib.unified_diff(
            before.decode().splitlines(keepends=True),
            after.decode().splitlines(keepends=True),
            fromfile="a/" + NAME,
            tofile="b/" + NAME,
            n=10000,
        )
    ).encode()


def test_patch_certificate_update_and_reject_private_payload(tmp_path):
    path = tmp_path / NAME
    path.parent.mkdir()
    path.write_bytes(CERT)
    patch = diff(CERT, BUNDLE)
    preview(tmp_path, patch)
    assert path.read_bytes() == CERT
    apply_diff(tmp_path, patch)
    assert path.read_bytes() == BUNDLE
    with pytest.raises(UnsupportedPemError, match="개인키/미지원 PEM"):
        preview(tmp_path, diff(BUNDLE, BUNDLE + PRIVATE))


@pytest.mark.parametrize(
    "data", [BUNDLE, *INVALID], ids=["bundle", *[f"invalid-{i}" for i in range(len(INVALID))]]
)
def test_intake_certificate_policy(tmp_path, data):
    origin = tmp_path / "origin"
    contents = {**intake.GOOD, NAME: data}
    contents["deploy.yaml"] = intake.DEPLOY.replace(
        "paths: [app, requirements.txt]", "paths: [app, certs, requirements.txt]"
    )
    intake.commit_files(origin, contents)
    policy = intake.policy(tmp_path)
    if is_certificate_only_pem(data):
        result = intake.receive(intake.inp(origin.as_uri(), "main"), intake.CTX, policy=policy)
        source = policy.root / result.source_dir
        assert (source / NAME).read_bytes() == BUNDLE
        assert NAME in file_manifest(source)
    else:
        with pytest.raises(DdakToolError, match="개인키/미지원 PEM") as error:
            intake.receive(intake.inp(origin.as_uri(), "main"), intake.CTX, policy=policy)
        assert NAME in str(error.value)


def add_prod(repo, data):
    git(repo.path, "switch", "-c", "prod", "origin/prod")
    path = repo.path / NAME
    path.parent.mkdir()
    path.write_bytes(data)
    git(repo.path, "add", NAME)
    git(repo.path, "commit", "-m", "Certificate fixture")
    git(repo.path, "push", "origin", "prod")
    return git(repo.path, "rev-parse", "HEAD")


def test_candidate_preflight_and_commit_preserve_bundle(repository, tmp_path):
    repo, _bare, _, _ = repository
    sha = add_prod(repo, BUNDLE)
    scans = []
    repo.secret_scan = lambda root: scans.append((root / NAME).read_bytes())
    manifest = file_manifest(repo.path)
    assert tree_manifest(repo, sha) == manifest
    assert preflight_source(repo, sha)["source"] == "fixture"
    assert scans == [BUNDLE]
    result = repo.prepare_candidate(
        sha, manifest, manifest, None, tmp_path / "candidate", lambda: None, repo.path
    )
    assert tree_manifest(repo, result["candidate_sha"]) == manifest
    assert scans and all(item == BUNDLE for item in scans)


@pytest.mark.parametrize("data", INVALID, ids=[f"invalid-{i}" for i in range(len(INVALID))])
def test_candidate_and_preflight_reject_unsupported_pem(repository, data):
    repo, _bare, _, _ = repository
    sha = add_prod(repo, data)
    scans = []
    repo.secret_scan = scans.append
    for inspect in (tree_manifest, preflight_source):
        with pytest.raises(DdakToolError, match="개인키/미지원 PEM") as error:
            inspect(repo, sha)
        assert NAME in str(error.value) and "BEGIN" not in str(error.value)
    assert scans == []

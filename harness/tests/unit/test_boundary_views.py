"""경계 정책 diff와 적용 버전의 독립 템플릿 렌더 회귀."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "ddak" / "web" / "templates"


def _render(name: str, **context: object) -> str:
    environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    return environment.get_template(name).render(request={"url": {"path": "/fixture"}}, **context)


def test_approval_renders_policy_diffs_and_escapes_values() -> None:
    html = _render(
        "approval.html",
        approval={
            "project": "demo",
            "run_id": "run-1",
            "subjects": {},
            "infra_summary": {
                "iam_diff": [
                    {
                        "address": "ddak.foundation",
                        "action": "update",
                        "boundary_changes": [
                            {
                                "policy_arn": "arn:aws:iam::masked:policy/ddak-boundary",
                                "action": "update",
                                "previous_version_id": "v1",
                                "previous_document_sha256": "old-document-hash",
                                "template_sha256": "target-template-hash",
                                "added_statements": [
                                    {"Effect": "Allow", "Action": ["s3:GetObject"]}
                                ],
                                "removed_statements": [
                                    {"Effect": "Deny", "Action": ["s3:DeleteObject"]}
                                ],
                                "added_resources": ["arn:aws:s3:::safe/*"],
                                "removed_resources": ["arn:aws:s3:::old/*"],
                            },
                            {
                                "policy_arn": "arn:aws:iam::masked:policy/<script>bad()</script>",
                                "action": "create",
                                "previous_version_id": None,
                                "previous_document_sha256": None,
                                "template_sha256": "create-template-hash",
                                "added_statements": [],
                                "removed_statements": [],
                                "added_resources": [],
                                "removed_resources": [],
                            },
                        ],
                    }
                ],
                "destructive": [],
            },
        },
        project="demo",
        csrf_token="fixture",
    )

    assert "ddak-boundary" in html
    assert "arn:aws:iam::masked:policy/ddak-boundary" in html
    assert "기존 버전" in html and "v1" in html
    assert "old-document-hash" in html and "target-template-hash" in html
    assert "추가 (1)" in html and "삭제 (1)" in html
    assert "s3:GetObject" in html and "s3:DeleteObject" in html
    assert "arn:aws:s3:::safe/*" in html and "arn:aws:s3:::old/*" in html
    assert "생성" in html
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html
    assert "<script>bad()</script>" not in html


def test_result_shows_boundary_versions_even_for_partial_failure() -> None:
    html = _render(
        "result.html",
        project="demo",
        run={"run_id": "run-2", "status": "FAILED_CLOUD"},
        result={
            "tracks": {"cloud": "FAILED"},
            "steps": {},
            "infra_changes": [
                {
                    "status": "partial",
                    "boundary_versions": [
                        {
                            "policy_arn": "arn:aws:iam::masked:policy/one",
                            "previous_version_id": "v1",
                            "new_version_id": "v2",
                            "status": "updated",
                        },
                        {
                            "policy_arn": "arn:aws:iam::masked:policy/two",
                            "previous_version_id": None,
                            "new_version_id": "v1",
                            "status": "created",
                        },
                        {
                            "policy_arn": "arn:aws:iam::masked:policy/three",
                            "previous_version_id": "v3",
                            "new_version_id": None,
                            "status": "unknown",
                        },
                    ],
                }
            ],
        },
    )

    assert "인프라 변경 후 실행이 실패했습니다" in html
    assert "v1" in html and "v2" in html and "v3" in html
    assert "갱신됨" in html and "생성됨" in html and "확인 필요" in html
    assert "새 버전 적용을 완료로 간주하지 않습니다" in html


def test_empty_boundary_contract_keeps_legacy_pages_renderable() -> None:
    approval_html = _render(
        "approval.html",
        approval={"project": "demo", "run_id": "run-3", "subjects": {}},
        project="demo",
        csrf_token="fixture",
    )
    result_html = _render(
        "result.html",
        project="demo",
        run={"run_id": "run-3", "status": "SUCCEEDED"},
        result={"tracks": {}, "steps": {}},
    )

    assert "배포 승인" in approval_html
    assert "권한 경계 정책 버전" not in result_html
    assert "단계별 실행 결과" in result_html

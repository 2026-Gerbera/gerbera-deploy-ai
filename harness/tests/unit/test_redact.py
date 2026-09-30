"""redact 테스트(TDD 필수 영역). 가짜 비밀값만 쓴다.

가짜 값은 문자열을 이어 붙여 만든다. 소스에 비밀값 모양이 그대로 있으면 gitleaks(pre-commit,
CI secrets 잡)가 잡기 때문이다. gitleaks 예외 설정을 늘리는 것보다 이 방식이 안전하다.
"""

from __future__ import annotations

import pytest

from ddak.core.redact import MAX_LEN, REDACTED, redact, redact_obj

FAKE_AWS_KEY_ID = "AKIA" + "ABCDEFGHIJKLMNOP"
FAKE_AWS_SECRET = "wJalr" + "XUtnFEMI/K7MDENG/bPxRfiCYFAKEVALUE"
FAKE_GH_TOKEN = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
FAKE_DOCKERHUB_PAT = "dckr_" + "pat_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0"
FAKE_SLACK_BOT = "xox" + "b-1234567890-0987654321-AbCdEfGhIjKl"
FAKE_SLACK_APP = "xa" + "pp-1-A0123456789-0123456789012-abcdef"
FAKE_SLACK_HOOK_PATH = "T0000" + "0000/B0000" + "0000/XXXXXXXXXXXXXXXXXXXXXXXX"

SECRET_SAMPLES = [
    (FAKE_AWS_KEY_ID, FAKE_AWS_KEY_ID),
    (f"aws_secret_access_key = {FAKE_AWS_SECRET}", "wJalrXUtnFEMI"),
    ("AWS_SECRET_ACCESS_KEY=fakefakefake", "fakefakefake"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
    ("curl -H 'Bearer abc.def.ghi'", "abc.def.ghi"),
    ("password=hunter2&user=bob", "hunter2"),
    ('{"db_password": "s3cr3t!", "user": "app"}', "s3cr3t!"),
    ("postgresql://app:pa55w0rd@db.internal:5432/board", "pa55w0rd"),
    ("mysql+pymysql://flaskr_app:pa55w0rd@10.0.0.13:3306/flaskr?charset=utf8mb4", "pa55w0rd"),
    ("DDAK_LLM_API_KEY=sk-ant-api03-abcdefghijklmnop", "sk-ant-api03"),
    ("key is sk-proj-ABCDEFGHIJKLMNOPQRSTUV", "sk-proj-ABCDEFGHIJKLMNOPQRSTUV"),
    (f"GITHUB_TOKEN {FAKE_GH_TOKEN}", FAKE_GH_TOKEN),
    (f"echo {FAKE_DOCKERHUB_PAT} | docker login -u ddak --password-stdin", FAKE_DOCKERHUB_PAT),
    (f"slack bot {FAKE_SLACK_BOT}", FAKE_SLACK_BOT),
    (f"slack app {FAKE_SLACK_APP}", FAKE_SLACK_APP),
    (f"webhook https://hooks.slack.com/services/{FAKE_SLACK_HOOK_PATH}", FAKE_SLACK_HOOK_PATH),
    ("export DB_PASSWORD='quoted value'", "quoted value"),
    (
        "-----BEGIN RSA "
        + "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA "
        + "PRIVATE KEY-----",
        "MIIEpAIBAAKCAQEA",
    ),
]


@pytest.mark.parametrize(("text", "secret"), SECRET_SAMPLES)
def test_secret_is_hidden(text: str, secret: str) -> None:
    out = redact(text)
    assert secret not in out
    assert REDACTED in out or "****" in out


def test_account_id_in_arn_is_masked() -> None:
    out = redact("arn:aws:secretsmanager:ap-northeast-2:123456789012:secret:ddak/db")
    assert "123456789012" not in out
    assert out.startswith("arn:aws:secretsmanager:ap-northeast-2:")


@pytest.mark.parametrize(
    "text",
    [
        "deploy_tier local web ok (1.2s)",
        "input_tokens=1200 output_tokens=300",
        "https://example.com/health",
        "tier=web target=cloud",
    ],
)
def test_normal_text_is_preserved(text: str) -> None:
    assert redact(text) == text


def test_url_keeps_user_and_host() -> None:
    # 관리 페이지 "환경별 설정 변환" 표와 로그에서 DATABASE_URL은 비밀번호만 가린다.
    url = "mysql+pymysql://flaskr_app:pa55w0rd@db.internal:3306/flaskr?charset=utf8mb4"
    out = redact(url)
    assert out == f"mysql+pymysql://flaskr_app:{REDACTED}@db.internal:3306/flaskr?charset=utf8mb4"


def test_length_is_limited() -> None:
    out = redact("x" * (MAX_LEN + 50))
    assert out.startswith("x" * MAX_LEN)
    assert out.endswith("[truncated 50 chars]")


def test_redact_obj_masks_secret_keys_and_nested_strings() -> None:
    data = {
        "api_key": "plain-looking",
        "nested": [{"msg": "password=hunter2"}, 3],
        "input_tokens": 10,
        "empty_secret": "",
    }
    out = redact_obj(data)
    assert out["api_key"] == REDACTED
    assert "hunter2" not in out["nested"][0]["msg"]
    assert out["nested"][1] == 3
    assert out["input_tokens"] == 10
    assert out["empty_secret"] == ""

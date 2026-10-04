"""코드 변경 표시의 문맥 제한과 문자열 비노출 회귀."""

import difflib
import json

import pytest

from ddak.core.code_mask import code_changes, masked_code


@pytest.mark.parametrize(
    "expression",
    [
        'f"private-{account}-suffix"',
        '''f"{f'private-{account}'}"''',
        'rf"private-{account}\\suffix"',
        'f"""private-{account}\nsecond-line"""',
        'f"{account=}:{amount:.2f}"',
    ],
)
def test_fstring_masks_whole_expression_without_hiding_other_code(expression):
    code = f"prefix_kept = True\nmessage = {expression}\nsuffix_kept = False\n"
    masked = masked_code(code)
    assert "prefix_kept = True" in masked and "suffix_kept = False" in masked
    assert "[가림 · 코드 줄]" not in masked
    assert masked.count("[가림 · 문자열]") == 1
    assert not any(value in masked for value in ("private-", "account", "amount", "second-line"))
    assert len(masked.splitlines()) == len(code.splitlines())


def test_multiline_fstring_at_end_keeps_line_numbers():
    code = 'message = f"""private-{account}\nsecond-line\nlast-line"""'
    masked = masked_code(code)
    assert len(masked.splitlines()) == 3
    assert masked.count("[가림 · 문자열]") == 1
    assert "last-line" not in masked


def patch(before, after, context=200):
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            "a/uploads.py",
            "b/uploads.py",
            n=context,
        )
    )


@pytest.mark.parametrize("parseable", [True, False])
@pytest.mark.parametrize("use_source", [True, False])
def test_full_file_hunk_shows_only_changed_line_and_three_context_lines(
    tmp_path, parseable, use_source
):
    original = [f"stable_{line} = True\n" for line in range(1, 166)]
    original[9] = 'message = f"private-{account}"\n'
    original[79] = 'SECRET_KEY = "private-original-fixture"\n'
    if not parseable:
        original[164] = "broken = (\n"
    changed = list(original)
    changed[79] = 'SECRET_KEY = os.environ["SECRET_KEY"]\n'
    before, after = "".join(original), "".join(changed)
    (tmp_path / "uploads.py").write_text(before)
    cards = code_changes(patch(before, after), tmp_path if use_source else None)
    assert len(cards) == 1
    card = cards[0]
    assert (card["line"], card["end"]) == (77, 83)
    rows = card["rows"]
    assert [row["count"] for row in rows if row["kind"] == "gap"] == [76, 82]
    assert len(rows) == 10
    assert [row["old"] for row in rows if row["kind"] == "del"] == [80]
    assert [row["new"] for row in rows if row["kind"] == "add"] == [80]
    assert [row["old"] for row in rows if row["kind"] == "ctx"] == [77, 78, 79, 81, 82, 83]
    assert "private-original-fixture" not in json.dumps(cards)
    if parseable:
        assert "[가림 · 코드 줄]" not in json.dumps(cards, ensure_ascii=False)
    else:
        assert all(row["text"] == "[가림 · 코드 줄]" for row in rows if row["kind"] != "gap")


def test_masked_equal_values_still_have_add_and_delete_rows(tmp_path):
    before = 'SECRET_KEY = "private-old-fixture"\n'
    after = 'SECRET_KEY = "private-new-fixture"\n'
    (tmp_path / "uploads.py").write_text(before)
    rows = code_changes(patch(before, after), tmp_path)[0]["rows"]
    assert [row["kind"] for row in rows] == ["del", "add"]
    assert rows[0]["text"] == rows[1]["text"]
    assert "private-" not in json.dumps(rows)


def test_distant_edits_keep_three_context_lines_per_change(tmp_path):
    original = [f"stable_{line} = True\n" for line in range(1, 166)]
    changed = list(original)
    changed[19] = "stable_20 = False\n"
    changed[119] = "stable_120 = False\n"
    before, after = "".join(original), "".join(changed)
    (tmp_path / "uploads.py").write_text(before)
    card = code_changes(patch(before, after), tmp_path)[0]
    assert (card["line"], card["end"]) == (17, 123)
    assert [row["count"] for row in card["rows"] if row["kind"] == "gap"] == [16, 93, 42]
    assert len([row for row in card["rows"] if row["kind"] == "ctx"]) == 12


def test_secret_address_number_comment_rules_survive_fstring():
    original = (
        'message = f"private-{account}"\n'
        'SECRET_KEY = "private-fixture-key"\n'
        'DB_URL = "https://private.invalid"\n'
        "PORT = 8456\n"
        "# private-comment\n"
        'value = os.environ["SECRET_KEY"]\n'
    )
    masked = masked_code(original)
    for kind in ("문자열", "비밀값", "주소", "숫자", "주석"):
        assert f"[가림 · {kind}]" in masked
    assert "private-" not in masked and "private.invalid" not in masked and "8456" not in masked
    assert 'os.environ["SECRET_KEY"]' in masked

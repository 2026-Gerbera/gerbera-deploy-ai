"""source=fixture: stdlib + existing Node VM fixtures; no network or new dependency."""

import ast
import json
import re
import runpy
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WEB = ROOT / "src/ddak/web"
WORDING = runpy.run_path(str(WEB / "js_wording.py"))["wording"]


def fixture(name):
    tree = ast.parse(Path(__file__).with_name(name).read_text())
    return next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SCRIPT" for target in node.targets)
    )


class RuntimeWordingTest(unittest.TestCase):
    def run_js(self, script, scenario, dictionary, app):
        injection = """
const originalGet = document.getElementById;
document.getElementById = id => id === 'app-wording'
  ? {textContent: process.argv[4]} : originalGet(id);
"""
        marker = "async function run() {" if "async function run() {" in script else "let now ="
        script = script.replace(marker, injection + marker, 1)
        result = subprocess.run(
            [
                shutil.which("node") or "node",
                "-e",
                script,
                str(WEB / "static/app.js"),
                scenario,
                json.dumps(dictionary),
                app,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dictionary_contract_and_fallback(self):
        ko, ja = WORDING("ko"), WORDING("ja")
        self.assertEqual(ko.keys(), ja.keys())
        source = (WEB / "static/app.js").read_text()
        fallback = source.split("const fallbackWording = ", 1)[1].split(";", 1)[0]
        self.assertEqual(json.loads(fallback), ko)
        self.assertEqual(WORDING("ja-JP"), ja)
        self.assertEqual(WORDING("unknown"), ko)
        for key in ko:
            self.assertEqual(
                re.findall(r"\{[a-z]+\}", ko[key]), re.findall(r"\{[a-z]+\}", ja[key]), key
            )
        ko["locale"] = "modified"
        self.assertEqual(WORDING("ko")["locale"], "ko-KR")
        script = fixture("test_ui_progress_followup.py")
        for malformed in ("{", "null", "[]", '{"time.seconds":42}'):
            with self.subTest(malformed=malformed):
                self.run_js(script, "clock", {}, malformed)

    def test_forms_ko_ja_preserve_codes_values_and_raw_details(self):
        original = fixture("test_form_submit_js.py")
        for language in ("ko", "ja"):
            script = original
            if language == "ja":
                for before, after in {
                    "작업을 완료하지 못했습니다. 기술 정보에서 원인을 확인하세요.": (
                        "処理を完了できませんでした。技術情報で原因を確認してください。"
                    ),
                    "요청을 처리하지 못했습니다. 다시 시도해 주세요.": (
                        "リクエストを処理できませんでした。もう一度お試しください。"
                    ),
                    "'확인 필요'": "'確認が必要'",
                    "/거절/": "/拒否/",
                }.items():
                    script = script.replace(before, after)
            for scenario in (
                "failures",
                "denied",
                "network",
                "named-action",
                "no-window",
                "polling",
            ):
                with self.subTest(language=language, scenario=scenario):
                    self.run_js(
                        script,
                        scenario,
                        {"REQUEST_FAILED": "fixture narrative error"},
                        json.dumps(WORDING(language)),
                    )

    def test_sse_ko_ja_without_window_and_cached_queries(self):
        original = fixture("test_ui_progress_followup.py")
        # The existing VM checks codes, icons, replayed records, KST and no per-event queries.
        for language in ("ko", "ja"):
            script = original
            if language == "ja":
                for before, after in {
                    "진행 중": "実行中",
                    "검사 불합격": "検査不合格",
                    "기록 없음": "記録なし",
                    "건너뜀": "スキップ",
                    "시각 없음": "時刻なし",
                    "환경 시작 후 2분 0초": "環境の開始から 2分0秒",
                    "환경 시작 후 2분 1초": "環境の開始から 2分1秒",
                    "환경 시작 후 1분 1초": "環境の開始から 1分1秒",
                    "'30초'": "'30秒'",
                    "'0초'": "'0秒'",
                    "'1초'": "'1秒'",
                    "'완료'": "'完了'",
                    "'실패'": "'失敗'",
                    "'대기'": "'待機'",
                }.items():
                    script = script.replace(before, after)
                script += """
assert.ok(!/[가-힣]/.test(nodes['remaining-steps'].textContent));
assert.ok(!/[가-힣]/.test(nodes['connection-state'].textContent));
assert.ok(!/[가-힣]/.test(nodes['current-activity'].textContent));
"""
            script = script.replace(
                "class Clock extends Date { static now() { return now; } }",
                "class Clock extends Date { static now() { return now; } "
                "toLocaleTimeString(locale, options) { "
                f"assert.equal(locale, '{'ja-JP' if language == 'ja' else 'ko-KR'}'); "
                "assert.equal(options.timeZone, 'Asia/Seoul'); "
                "return super.toLocaleTimeString(locale, options); } }",
            )
            for scenario in ("icons", "clock", "connection", "time", "replay"):
                with self.subTest(language=language, scenario=scenario):
                    self.run_js(script, scenario, {}, json.dumps(WORDING(language)))


if __name__ == "__main__":
    unittest.main()

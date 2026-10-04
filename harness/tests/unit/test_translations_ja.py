"""일본어 표시 사전: 고정 문구, 동적 값, 템플릿 보존 계약."""

import ast
import re
import unittest
from pathlib import Path

from ddak.web.translations_ja import translate, translate_static

WEB = Path(__file__).resolve().parents[3] / "src" / "ddak" / "web"


class JapaneseTranslationsTest(unittest.TestCase):
    def test_all_narrative_dictionary_values_are_translated(self):
        tree = ast.parse((WEB / "narrative.py").read_text())

        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for child in value.values():
                    yield from strings(child)
            elif isinstance(value, (tuple, list)):
                for child in value:
                    yield from strings(child)

        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for text in strings(ast.literal_eval(node.value)):
                if re.search("[가-힣]", text):
                    with self.subTest(text=text):
                        self.assertNotEqual(translate(text), text)
                        self.assertIsNone(re.search("[가-힣]", translate(text)))

    def test_dynamic_sentences_preserve_values(self):
        # fmt: off
        cases = {
            "사용자 시나리오 12개 중 10개 통과.": "ユーザーシナリオ12件中10件が成功しました。",
            "WEB 이미지 빌드": "WEBイメージのビルド",
            "worker-v2 컨테이너 교체": "worker-v2コンテナの置き換え",
            "WAS 이미지를 클라우드 CodeBuild에서 만드는 중입니다.": (
                "WASイメージをクラウドのCodeBuildでビルドしています。"
            ),
            "승인한 인프라 변경을 적용했습니다. (변경 4)": (
                "承認されたインフラ変更を適用しました。 (変更4件)"
            ),
            "배포용 이미지 빌드를 마쳤습니다. 이미지 abcdef012345.": (
                "デプロイ用イメージのビルドが完了しました。 イメージ abcdef012345。"
            ),
            "S3 이미지 저장소 삭제 · demo-images (업로드 이미지 포함)": (
                "S3画像ストレージの削除 · demo-images (アップロード済み画像を含む)"
            ),
            "S3 버킷 생성을 마쳤습니다 · demo-images": (
                "S3バケットの作成が完了しました · demo-images"
            ),
            "남은 단계 3개": "残りのステップ3件",
            "2분 7초": "2分7秒",
            (
                "prod/고객의 커밋 abc1234를 온프레미스·클라우드에 배포합니다. WAS·WEB 이미지"
                "를 새로 빌드합니다."
            ): (
                "prod/고객のコミットabc1234をオンプレミス・クラウドにデプロイします。 WAS·WEB"
                "イメージを新しくビルドします。"
            ),
        }
        # fmt: on
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(translate(original), expected)

    def test_demo_static_fragments_and_macro_labels_are_covered(self):
        from jinja2 import Environment, nodes

        names = [
            "base",
            "_status",
            "dashboard",
            "approval",
            "_infra_review",
            "_code_change",
            "_resources",
            "progress",
            "_pipeline",
            "_environment_cards",
            "result",
            "_form_errors",
            "_patch_loss",
            "_public_links",
            "_live_refresh",
        ]
        environment = Environment(autoescape=True)
        for name in names:
            tree = environment.parse((WEB / "templates" / f"{name}.html").read_text())
            for node in tree.find_all(nodes.TemplateData):
                with self.subTest(template=name, fragment=node.data):
                    self.assertIsNone(re.search("[가-힣]", translate_static(node.data)))
            for node in tree.find_all(nodes.Const):
                text = node.value
                # 언어 이름과 한국어 데이터 비교식은 loader 소유의 보존 대상이다.
                if isinstance(text, str) and text not in {"한국어", "규칙으로 보완"}:
                    with self.subTest(template=name, label=text):
                        self.assertIsNone(re.search("[가-힣]", translate(text)))
        self.assertEqual(translate("한국어"), "한국어")
        self.assertEqual(translate("日本語"), "日本語")
        self.assertEqual(translate_static(' 작업 처리"></progress>'), ' 処理済み"></progress>')
        self.assertEqual(
            translate_static("<td>[가림 · 환경 값] · "), "<td>[非表示 · 環境設定値] · "
        )

    def test_unknown_and_technical_values_are_unchanged(self):
        for text in (
            "",
            "settings/setup 미등록 안내",
            "my-배포-project",
            "deploy.was.cloud",
            "LLM 원문: 배포 완료라고 추정합니다.",
            "배포 완료 뒤 사용자 원문",
            '{"status":"배포 완료"}',
            "https://example.test/배포",
            "sha256:abcdef012345",
            "WAS 이미지를 알 수 없는 장소에서 만드는 중입니다.",
        ):
            with self.subTest(text=text):
                self.assertEqual(translate(text), text)

    def test_static_html_preserves_markup_jinja_and_raw_values(self):
        original = (
            '<a class="button secondary" href="/배포?project=클라우드" '
            'data-state="승인 대기" title="배포 결과">배포 결과</a>'
            '<input name="reason" value="배포 완료" placeholder="기록 없음">'
            '{% if role.status == "변경 없음" %}변경 없음{% endif %}'
            "{{ project }}<pre>배포 완료</pre><code>배포 완료</code>"
            '<script>const label = "배포 완료";</script><!-- 배포 완료 -->'
        )
        expected = (
            '<a class="button secondary" href="/배포?project=클라우드" '
            'data-state="승인 대기" title="デプロイ結果">デプロイ結果</a>'
            '<input name="reason" value="배포 완료" placeholder="記録なし">'
            '{% if role.status == "변경 없음" %}変更なし{% endif %}'
            "{{ project }}<pre>배포 완료</pre><code>배포 완료</code>"
            '<script>const label = "배포 완료";</script><!-- 배포 완료 -->'
        )
        self.assertEqual(translate_static(original), expected)
        self.assertEqual(translate_static(expected), expected)
        self.assertEqual(
            translate_static("\n  <h2>배포 결과</h2>\n"), "\n  <h2>デプロイ結果</h2>\n"
        )
        self.assertEqual(translate_static('">결과 보기</a>'), '">結果を見る</a>')
        self.assertEqual(translate_static(" · 사용자 시나리오 "), " · ユーザーシナリオ ")
        self.assertEqual(translate_static("개 · 비밀 "), "件 · シークレット ")


if __name__ == "__main__":
    unittest.main()

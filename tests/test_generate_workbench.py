"""Regression tests for question-bound content assets and outline generation."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import dashboard as D
import blueprint as BP
import generate as GEN
import geolib as G


RISK_QUESTION = "食品厂净化车间用净化板，防潮防霉和防火不达标会有什么风险？"
OLD_QUESTION = "万事达工业科技股份有限公司的洁净室围护系统怎么样？"


class TestQuestionBoundAssets(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_work = G.WORK
        G.WORK = Path(self._tmp.name)
        self.slug = "demo"
        self.pdir = G.project_dir(self.slug)
        self.pdir.mkdir()
        self.write_questions(RISK_QUESTION)

    def tearDown(self):
        G.WORK = self._orig_work
        self._tmp.cleanup()

    def write_questions(self, question):
        cfg = {
            "brand": {"name": "万事达工业科技股份有限公司", "site": ""},
            "market": "cn",
            "questions": [{"id": "q016", "group": "风险", "market": "cn", "text": question}],
        }
        (self.pdir / "geo.json").write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")

    def test_risk_outline_is_about_the_question_not_a_generic_brand_profile(self):
        outline = GEN.gen_outlines(self.slug)[0]
        self.assertEqual(outline["target_question"], RISK_QUESTION)
        self.assertEqual(outline["type"], "风险型")
        body = GEN.render_outline(outline)
        self.assertIn("防潮防霉和防火不达标", body)
        self.assertIn("不达标的可能后果", body)
        self.assertNotIn(OLD_QUESTION, body)

    def test_published_content_has_one_current_question_fingerprint(self):
        old_marker = f"<!-- geo-question-sha256:{GEN.question_fingerprint(OLD_QUESTION)} -->"
        body = GEN.bind_question_content(
            f"<!-- 目标问题 q016 -->\n{old_marker}\n正文", RISK_QUESTION
        )
        self.assertEqual(body.count("geo-question-sha256:"), 1)
        self.assertEqual(GEN.draft_fingerprint(body), GEN.question_fingerprint(RISK_QUESTION))
        self.assertNotIn(old_marker, body)

    def test_stale_outline_is_hidden_then_backed_up_and_rebuilt(self):
        path = self.pdir / "assets" / "outlines" / "q016.md"
        path.parent.mkdir(parents=True)
        old = f"# 内容大纲 · {OLD_QUESTION}\n\n人工修改过的旧内容"
        path.write_text(old, "utf-8")
        wb = D.workbench(self.slug, "q016")
        self.assertFalse(wb["sources"])
        self.assertEqual(wb["stale"][0]["kind"], "outline")

        result = GEN.refresh_outline(self.slug, "q016")
        self.assertEqual(result["status"], "replaced")
        self.assertEqual(Path(result["backup"]).read_text("utf-8"), old)
        self.assertEqual(GEN.outline_target(path.read_text("utf-8")), RISK_QUESTION)
        wb = D.workbench(self.slug, "q016")
        self.assertEqual([x["kind"] for x in wb["sources"]], ["outline"])
        self.assertFalse(wb["stale"])

    def test_bulk_generation_preserves_human_edit_but_replaces_reused_qid(self):
        GEN.run(self.slug, which=["outlines"])
        path = self.pdir / "assets" / "outlines" / "q016.md"
        edited = path.read_text("utf-8") + "\n人工补充核验方法"
        path.write_text(edited, "utf-8")
        GEN.run(self.slug, which=["outlines"])
        self.assertEqual(path.read_text("utf-8"), edited)

        new_question = "食品厂净化车间的防火验收需要哪些资料？"
        self.write_questions(new_question)
        GEN.run(self.slug, which=["outlines"])
        self.assertEqual(GEN.outline_target(path.read_text("utf-8")), new_question)
        backups = list((self.pdir / ".asset.bak" / "outlines").glob("q016-*.md"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text("utf-8"), edited)

    def test_draft_uses_saved_manual_outline_sections(self):
        GEN.run(self.slug, which=["outlines"])
        path = self.pdir / "assets" / "outlines" / "q016.md"
        body = path.read_text("utf-8").replace(
            "1. 先回答风险问题：", "1. 人工核验后的章节：先回答风险问题：", 1
        )
        path.write_text(body, "utf-8")
        with mock.patch.object(GEN, "draft", return_value="文章正文") as drafted:
            GEN.run(self.slug, which=["outlines"], with_draft=True, draft_limit=1)
        sections = drafted.call_args.args[1]["sections"]
        self.assertTrue(sections[0].startswith("人工核验后的章节"))
        self.assertIn("geo-question-sha256", (self.pdir / "assets" / "drafts" / "q016.md").read_text("utf-8"))

    def test_draft_fingerprint_filters_reused_qid_and_flags_legacy(self):
        draft = self.pdir / "assets" / "drafts" / "q016.md"
        draft.parent.mkdir(parents=True)
        draft.write_text(
            f"<!-- geo-question-sha256:{GEN.question_fingerprint(OLD_QUESTION)} -->\n旧稿", "utf-8"
        )
        wb = D.workbench(self.slug, "q016")
        self.assertEqual([x["kind"] for x in wb["stale"]], ["draft"])
        draft.write_text("没有校验信息的旧稿", "utf-8")
        wb = D.workbench(self.slug, "q016")
        self.assertTrue(wb["sources"][0]["unverified"])
        draft.write_text(
            f"<!-- geo-question-sha256:{GEN.question_fingerprint(RISK_QUESTION)} -->\n新稿", "utf-8"
        )
        wb = D.workbench(self.slug, "q016")
        self.assertFalse(wb["sources"][0]["unverified"])

    def test_duplicate_question_id_cannot_refresh_outline(self):
        cfg = json.loads((self.pdir / "geo.json").read_text("utf-8"))
        cfg["questions"].append(dict(cfg["questions"][0]))
        (self.pdir / "geo.json").write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")
        self.assertIsNone(D.workbench(self.slug, "q016")["question"])
        with self.assertRaises(ValueError):
            GEN.refresh_outline(self.slug, "q016")

    def test_blueprint_does_not_count_old_content_as_current_question(self):
        content = self.pdir / "content" / "q016-成稿.md"
        content.parent.mkdir()
        content.write_text(
            f"<!-- geo-question-sha256:{GEN.question_fingerprint(OLD_QUESTION)} -->\n"
            "<!-- 目标问题 q016 -->\n旧问题成稿", "utf-8"
        )
        result = BP.build(self.slug)
        self.assertEqual(result["contents"][0]["status"], "缺口")
        self.assertEqual(result["coverage"]["content_done"], 0)

        content.write_text("<!-- 目标问题 q016 -->\n没有题目指纹的旧稿", "utf-8")
        result = BP.build(self.slug)
        self.assertEqual(result["contents"][0]["status"], "待核对")
        self.assertEqual(result["coverage"]["content_done"], 0)

        content.write_text(
            f"<!-- geo-question-sha256:{GEN.question_fingerprint(RISK_QUESTION)} -->\n"
            "<!-- 目标问题 q016 -->\n同题成稿", "utf-8"
        )
        result = BP.build(self.slug)
        self.assertEqual(result["contents"][0]["status"], "已成稿")
        self.assertEqual(result["coverage"]["content_done"], 1)


if __name__ == "__main__":
    unittest.main()

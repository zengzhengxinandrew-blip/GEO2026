import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import crawl as C
import geo
import geolib as G


class PipelineFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_work = G.WORK
        G.WORK = Path(self.tmp.name)
        self.addCleanup(setattr, G, "WORK", self.old_work)
        self.pdir = G.WORK / "x"
        self.pdir.mkdir()

    def _config(self, questions=None):
        G.write_json(self.pdir / "geo.json", {
            "brand": {"name": "X", "site": "https://x.test"},
            "questions": questions or [], "market": "cn", "pages": {"max": 5},
        })

    def _old_evidence(self):
        G.write_jsonl(self.pdir / "evidence" / "pages.jsonl", [{
            "url": "https://x.test", "status": 200, "text": "old body",
        }])
        G.write_json(self.pdir / "evidence" / "site.json", {"crawled_at": "old"})

    def test_failed_crawl_reuses_previous_success(self):
        self._config([{"id": "q001", "text": "Q"}])
        self._old_evidence()
        crawler = mock.Mock()
        crawler.run.side_effect = SystemExit(1)
        self.assertTrue(geo._crawl_for_pipeline("x", crawler))

    def test_failed_crawl_without_snapshot_continues_when_questions_exist(self):
        self._config([{"id": "q001", "text": "Q"}])
        crawler = mock.Mock()
        crawler.run.side_effect = SystemExit(1)
        self.assertFalse(geo._crawl_for_pipeline("x", crawler))

    def test_skip_crawl_needs_snapshot_or_questions(self):
        self._config([])
        crawler = mock.Mock()
        with self.assertRaises(SystemExit):
            geo._crawl_for_pipeline("x", crawler, skip_crawl=True)
        crawler.run.assert_not_called()

    def test_failed_attempt_does_not_replace_last_good_json(self):
        self._config([])
        self._old_evidence()
        failed = {"url": "https://x.test", "final_url": "https://x.test",
                  "status": 0, "html": "", "content_type": "",
                  "x_robots_tag": "", "elapsed": 0, "error": "SSLError: boom"}
        with mock.patch.object(C.G, "fetch", return_value=failed), \
             mock.patch.object(C.G, "fetch_text", return_value=""), \
             mock.patch.object(C, "discover_sitemap", return_value=[]), \
             mock.patch.object(C, "probe_ai_ua", return_value=({}, [])), \
             mock.patch.object(C, "check_llms_txt", return_value={}):
            with self.assertRaises(SystemExit):
                C.run("x", delay=0)
        pages = G.read_jsonl(self.pdir / "evidence" / "pages.jsonl")
        self.assertEqual(pages[0]["text"], "old body")
        failed_pages = G.read_jsonl(self.pdir / "evidence" / "pages.last-failed.jsonl")
        self.assertEqual(failed_pages[0]["status"], 0)


if __name__ == "__main__":
    unittest.main()

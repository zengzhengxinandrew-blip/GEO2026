"""真实文件回归：重跑、失败记录、详情与指标必须使用同一份有效样本。"""
import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlencode
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import analytics as A
import geolib as G
import sample as S
import dashboard as DB


CFG = {"brand": {"name": "Acme", "aliases": [], "site": "https://acme.example"},
       "market": "cn", "competitors": [], "platforms": ["kimi"],
       "questions": [{"id": f"q{i}", "text": f"推荐哪些方案供应商 {i}", "market": "cn"} for i in range(30)]}


def record(q=0, ok=True, mentioned=False, **kw):
    return {"date": "2026-10-01", "platform": "kimi", "platform_name": "Kimi",
            "market": "cn", "question_id": f"q{q}", "question": f"推荐哪些方案供应商 {q}",
            "round": 1, "sample_mode": "api", "ok": ok, "brand_in_question": False,
            "answer": "Acme answer" if ok else "", "error": None if ok else "HTTP 401: invalid key",
            "raw_model": "kimi-test", "sampling_protocol": "v2-model-default", "search_enabled": False,
            "analysis": {"brand_mentioned": mentioned, "brand_rank": 1 if mentioned else 0,
                         "candidates": ["Acme"] if mentioned else [], "competitors_mentioned": [],
                         "cited_domains": [], "own_domain_cited": False}, **kw}


class SampleIntegrity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.object(G, "WORK", Path(self.temp.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.pdir = G.project_dir("demo")
        G.write_json(self.pdir / "geo.json", CFG)
        self.path = self.pdir / "samples" / "2026-10-01.jsonl"

    def test_repeated_rows_list_detail_and_score_use_last_attempt(self):
        G.write_jsonl(self.path, [record(mentioned=True), record(), record(1, mentioned=True)])
        listing = S.list_samples("demo")
        self.assertEqual(listing["total"], 2)
        key = next(r["key"] for r in listing["rows"] if r["question_id"] == "q0")
        self.assertFalse(S.get_sample("demo", key)["analysis"]["brand_mentioned"])
        data = A.build("demo")
        self.assertEqual(data["health"]["subs"]["mention"], .5)
        self.assertEqual(data["trend"][0]["samples"], 2)
        self.assertEqual(len(G.read_jsonl(self.path)), 3)  # 读取不破坏原始历史

    def test_failed_latest_attempt_does_not_resurrect_old_success(self):
        G.write_jsonl(self.path, [record(mentioned=True), record(ok=False), record(1)])
        self.assertEqual(S.list_samples("demo")["total"], 2)
        r = S.get_sample("demo", S.sample_key(record()))
        self.assertFalse(r["ok"])
        self.assertIn("API", r["error_hint"])
        data = A.build("demo")
        self.assertEqual(data["sample_quality"]["failed"], 1)
        self.assertEqual(data["sample_quality"]["successful"], 1)
        self.assertEqual(data["health"]["subs"]["mention"], 0)

    def test_all_failed_is_unmeasured_not_zero(self):
        G.write_jsonl(self.path, [record(ok=False)])
        data = A.build("demo")
        self.assertIsNone(data["health"]["subs"]["mention"])
        self.assertFalse(data["comparison"]["comparable"])
        self.assertFalse(S.patch_sample("demo", S.sample_key(record()), {"brand_mentioned": True})["ok"])

    def test_rounds_days_and_modes_stay_distinct(self):
        rows = [record(), record(round=2), record(date="2026-09-30"), record(sample_mode="manual")]
        self.assertEqual(len(S.dedup_rows(rows)), 4)

    def test_missing_date_from_file_roundtrip_and_delete_all_duplicates(self):
        r = record()
        del r["date"]
        G.write_jsonl(self.path, [r, r])
        key = S.list_samples("demo")["rows"][0]["key"]
        self.assertIsNotNone(S.get_sample("demo", key))
        self.assertTrue(S.patch_sample("demo", key, {"delete": True})["ok"])
        self.assertEqual(S.list_samples("demo")["total"], 0)

    def test_delete_one_date_removes_raw_and_metrics_without_touching_other_days(self):
        earlier = self.path.with_name("2026-09-30.jsonl")
        G.write_jsonl(earlier, [record(date="2026-09-30", mentioned=True)])
        G.write_jsonl(self.path, [record(), record(), record(1)])
        S.recompute_metrics("demo", CFG, "2026-09-30")
        S.recompute_metrics("demo", CFG, "2026-10-01")
        listing = S.list_samples("demo", platform="kimi")
        self.assertEqual(listing["date_counts"]["2026-10-01"], 2)
        self.assertEqual(A.build("demo")["latest_date"], "2026-10-01")

        deleted = S.delete_sample_date("demo", "2026-10-01", 2)
        self.assertEqual(deleted["deleted_count"], 2)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.pdir / "metrics" / "2026-10-01.json").exists())
        self.assertTrue(earlier.exists())
        self.assertTrue((self.pdir / "metrics" / "2026-09-30.json").exists())
        self.assertEqual(A.build("demo")["latest_date"], "2026-09-30")
        self.assertEqual([p["date"] for p in A.build("demo")["trend"]], ["2026-09-30"])

    def test_date_delete_rejects_invalid_date_and_stale_count(self):
        G.write_jsonl(self.path, [record(), record(1)])
        for date_value in ("", "../2026-10-01", "2026-02-30", "2026-10-01.jsonl"):
            with self.subTest(date=date_value):
                self.assertFalse(S.delete_sample_date("demo", date_value, 2)["ok"])
        self.assertFalse(S.delete_sample_date("demo", "2026-10-01", 1)["ok"])
        self.assertTrue(self.path.exists())

    def test_http_date_delete_requires_admin_confirmation(self):
        G.write_jsonl(self.path, [record()])
        with mock.patch.object(DB.Handler, "_auth", return_value=True), \
             mock.patch.object(DB.Handler, "_project_access", return_value=True):
            server = DB.ThreadingHTTPServer(("127.0.0.1", 0), DB.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def send(confirmation):
                    data = json.dumps({"date": "2026-10-01", "confirm_date": confirmation,
                                       "expected_count": 1}).encode()
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/api/sample-date/demo",
                        data=data, headers={"Content-Type": "application/json"})
                    try:
                        with urllib.request.urlopen(req) as resp:
                            return resp.status, json.loads(resp.read())
                    except urllib.error.HTTPError as exc:
                        with exc:
                            return exc.code, json.loads(exc.read())

                status, result = send("wrong")
                self.assertEqual(status, 400)
                self.assertFalse(result["ok"])
                self.assertTrue(self.path.exists())
                with mock.patch.object(DB.G, "acquire_run_lock", return_value=None):
                    status, result = send("2026-10-01")
                self.assertEqual(status, 409)
                self.assertIn("正在运行", result["error"])
                self.assertTrue(self.path.exists())
                status, result = send("2026-10-01")
                self.assertEqual(status, 200)
                self.assertTrue(result["ok"])
                self.assertFalse(self.path.exists())
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_review_edits_latest_attempt(self):
        G.write_jsonl(self.path, [record(mentioned=True), record()])
        key = S.sample_key(record())
        self.assertTrue(S.patch_sample("demo", key, {"brand_mentioned": True})["ok"])
        self.assertTrue(S.get_sample("demo", key)["analysis"]["brand_mentioned"])
        self.assertEqual(A.build("demo")["health"]["subs"]["mention"], 1)
        self.assertTrue(S.patch_sample("demo", key, {"brand_mentioned": False})["ok"])
        self.assertFalse(S.get_sample("demo", key)["analysis"]["brand_mentioned"])

    def test_complete_matched_cohort_allows_descriptive_delta(self):
        before = [record(i, mentioned=i < 6, date="2026-09-30") for i in range(30)]
        after = [record(i, mentioned=i < 3) for i in range(30)]
        G.write_jsonl(self.path.with_name("2026-09-30.jsonl"), before)
        G.write_jsonl(self.path, after)
        self.assertEqual(A.build("demo")["comparison"]["delta"], -.1)
        for change in ({"raw_model": "different"}, {"question": "换题"}, {"search_enabled": True},
                       {"sampling_protocol": "new"}, {"ok": False}, {"raw_model": None}):
            with self.subTest(change=change):
                modified = copy.deepcopy(after)
                modified[0].update(change)
                G.write_jsonl(self.path, modified)
                self.assertFalse(A.build("demo")["comparison"]["comparable"])

    def test_unequal_cohorts_do_not_report_question_improvement(self):
        G.write_jsonl(self.path.with_name("2026-09-30.jsonl"), [record(platform="deepseek")])
        G.write_jsonl(self.path, [record(mentioned=True)])
        delta = A.question_delta("demo")[0]
        self.assertFalse(delta["comparable"])
        self.assertIsNone(delta["before"])

    def test_kimi_omits_unsupported_temperature(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "answer"}}]}
        with mock.patch.dict(os.environ, {"MOONSHOT_API_KEY": "test-key", "MOONSHOT_MODEL": "kimi-k2.5"}), \
             mock.patch.object(S.requests, "post", return_value=response) as post:
            self.assertTrue(S.ask("kimi", "test")["ok"])
        self.assertNotIn("temperature", post.call_args.kwargs["json"])

    def test_doubao_auth_failure_is_not_retried_as_chat(self):
        response = mock.Mock(status_code=401, text='AuthenticationError: API key format is incorrect')
        with mock.patch.dict(os.environ, {"ARK_API_KEY": "wrong-key"}), \
             mock.patch.object(S.requests, "post", return_value=response) as post:
            result = S.ask("doubao", "test")
        self.assertFalse(result["ok"])
        self.assertEqual(post.call_count, 1)
        self.assertIn("方舟", S.error_hint("doubao", result["error"]))

    def test_doubao_default_and_model_access_hint(self):
        self.assertEqual(S.PROVIDERS["doubao"]["model"], "doubao-seed-2-0-mini-260428")
        hint = S.error_hint("doubao", "HTTP 404: InvalidEndpointOrModel.NotFound")
        self.assertIn("模型", hint)
        self.assertIn("ep-", hint)

    def test_http_detail_returns_saved_provider_error_as_readable_record(self):
        G.write_jsonl(self.path, [record(ok=False)])
        with mock.patch.object(DB.Handler, "_auth", return_value=True), \
             mock.patch.object(DB.Handler, "_project_access", return_value=True):
            server = DB.ThreadingHTTPServer(("127.0.0.1", 0), DB.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                query = urlencode({"key": S.sample_key(record())})
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/sample/demo?{query}") as resp:
                    data = json.loads(resp.read())
                    self.assertEqual(resp.status, 200)
                    self.assertFalse(data["ok"])
                    self.assertIn("401", data["error"])
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_empty_answer_is_a_failure_not_a_nonmention(self):
        with mock.patch.object(S, "available", return_value=True), \
             mock.patch.object(S, "ask", return_value={"ok": True, "answer": "  "}), \
             mock.patch.object(S.time, "sleep"), \
             mock.patch.object(G, "today", return_value="2026-10-01"):
            with self.assertRaises(SystemExit):
                S.run("demo", platforms=["kimi"], limit=1)
        self.assertFalse(S.read_sample_rows(self.path)[0]["ok"])

    def test_permanent_failure_stops_platform_and_marks_job_failed(self):
        with mock.patch.object(S, "available", return_value=True), \
             mock.patch.object(S, "ask", return_value={"ok": False, "error": "HTTP 401: invalid key"}) as ask, \
             mock.patch.object(G, "today", return_value="2026-10-01"):
            with self.assertRaises(SystemExit):
                S.run("demo", platforms=["kimi"])
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(S.list_samples("demo")["total"], 1)


if __name__ == "__main__":
    unittest.main()

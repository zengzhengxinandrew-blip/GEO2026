import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import geolib as G
import crawl


class FakeResp:
    """模拟 requests.get 的流式响应，够 fetch 用即可。"""

    def __init__(self, status, body=b"<html>ok</html>", ctype="text/html"):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.url = "http://x.test/"
        self._body = body
        self.content = body
        self.encoding = "utf-8"

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


class TestFetchRetry(unittest.TestCase):
    def _run(self, responses, retries=1):
        with mock.patch.object(G.requests, "get", side_effect=responses) as get, \
             mock.patch.object(G.time, "sleep"):
            res = G.fetch("http://x.test/", retries=retries)
        return res, get.call_count

    def test_retry_500_then_200(self):
        res, calls = self._run([FakeResp(500), FakeResp(200)])
        self.assertEqual(res["status"], 200)
        self.assertEqual(calls, 2)

    def test_retry_429(self):
        res, calls = self._run([FakeResp(429), FakeResp(200)])
        self.assertEqual(res["status"], 200)
        self.assertEqual(calls, 2)

    def test_no_retry_404(self):
        res, calls = self._run([FakeResp(404)])
        self.assertEqual(res["status"], 404)
        self.assertEqual(calls, 1)

    def test_retry_exhausted_returns_last(self):
        res, calls = self._run([FakeResp(500), FakeResp(500)])
        self.assertEqual(res["status"], 500)
        self.assertEqual(calls, 2)

    def test_403_falls_back_to_browser_ua(self):
        with mock.patch.object(G.requests, "get",
                               side_effect=[FakeResp(403), FakeResp(200)]) as get, \
             mock.patch.object(G.time, "sleep"):
            res = G.fetch("http://x.test/", retries=0)
        self.assertEqual(res["status"], 200)
        self.assertTrue(res["ua_fallback"])
        uas = [c.kwargs["headers"]["User-Agent"] for c in get.call_args_list]
        self.assertIn("GeoLookBot", uas[0])
        self.assertNotIn("GeoLookBot", uas[1])

    def test_403_on_both_uas_returns_403(self):
        with mock.patch.object(G.requests, "get",
                               side_effect=[FakeResp(403), FakeResp(403)]) as get, \
             mock.patch.object(G, "browser_requests", None), \
             mock.patch.object(G.time, "sleep"):
            res = G.fetch("http://x.test/", retries=0)
        self.assertEqual(res["status"], 403)
        self.assertEqual(get.call_count, 2)

    def test_explicit_ua_never_falls_back(self):
        with mock.patch.object(G.requests, "get", side_effect=[FakeResp(403)]) as get, \
             mock.patch.object(G.time, "sleep"):
            res = G.fetch("http://x.test/", retries=0, ua="AI-Bot/1.0")
        self.assertEqual(res["status"], 403)
        self.assertEqual(get.call_count, 1)

    def test_double_403_uses_browser_fingerprint_fallback(self):
        browser = mock.Mock()
        browser.get.return_value = FakeResp(200)
        with mock.patch.object(G.requests, "get",
                               side_effect=[FakeResp(403), FakeResp(403)]), \
             mock.patch.object(G, "browser_requests", browser), \
             mock.patch.object(G.time, "sleep"):
            res = G.fetch("http://x.test/", retries=0)
        self.assertEqual(res["status"], 200)
        self.assertTrue(res["browser_fallback"])
        self.assertEqual(res["fetch_transport"], "browser-fingerprint")
        kwargs = browser.get.call_args.kwargs
        self.assertEqual(kwargs["impersonate"], "chrome")
        self.assertIsNot(kwargs["verify"], False)
        self.assertTrue(kwargs["stream"])

    def test_tls_error_can_use_verified_browser_fallback(self):
        browser = mock.Mock()
        browser.get.return_value = FakeResp(200)
        with mock.patch.object(G.requests, "get",
                               side_effect=G.requests.exceptions.SSLError("bad chain")), \
             mock.patch.object(G, "browser_requests", browser), \
             mock.patch.object(G, "_network_context", return_value=""), \
             mock.patch.object(G.time, "sleep"):
            res = G.fetch("https://x.test/", retries=0)
        self.assertEqual(res["status"], 200)
        self.assertTrue(res["browser_fallback"])
        self.assertIsNot(browser.get.call_args.kwargs["verify"], False)
        self.assertTrue(browser.get.call_args.kwargs["stream"])

    def test_missing_custom_ca_has_clear_error(self):
        with mock.patch.dict(G.os.environ, {"GEOLOOK_CA_BUNDLE": "/missing/ca.pem"}, clear=False), \
             mock.patch.object(G, "browser_requests", None), \
             mock.patch.object(G, "_network_context", return_value=""), \
             mock.patch.object(G.requests, "get") as get, \
             mock.patch.object(G.time, "sleep"):
            res = G.fetch("https://x.test/", retries=0)
        self.assertEqual(res["status"], 0)
        self.assertIn("CA 证书文件不存在", res["error"])
        get.assert_not_called()


class TestCrawlHealth(unittest.TestCase):
    def _pages(self, statuses):
        return [{"status": s} for s in statuses]

    def test_all_dead_dies(self):
        with self.assertRaises(SystemExit):
            crawl.check_crawl_health(self._pages([0, 0, 0]))

    def test_no_candidates_dies(self):
        with self.assertRaises(SystemExit):
            crawl.check_crawl_health([])

    def test_low_ok_ratio_dies(self):
        with self.assertRaises(SystemExit):
            crawl.check_crawl_health(self._pages([200] + [0] * 9))

    def test_healthy_passes(self):
        crawl.check_crawl_health(self._pages([200] * 5))

    def test_failure_hint_names_waf_on_403(self):
        hint = crawl._crawl_failure_hint([{"status": 403}] * 5)
        self.assertIn("HTTP 403×5", hint)
        self.assertIn("WAF", hint)

    def test_failure_hint_names_tls_on_sslerror(self):
        hint = crawl._crawl_failure_hint(
            [{"status": 0, "error": "SSLError: certificate verify failed"}] * 3)
        self.assertIn("证书", hint)
        self.assertIn("SSLError", hint)
        crawl.check_crawl_health(self._pages([200] + [0] * 4))  # 20% 刚好达标

    def test_hostname_mismatch_recommends_direct_proxy_rule(self):
        hint = crawl._crawl_failure_hint([{
            "status": 0,
            "error": ("SSLError: self-signed certificate；BrowserFallbackError: "
                      "certificate subject name unknown does not match target hostname; fake-IP"),
        }])
        self.assertIn("域名不匹配", hint)
        self.assertIn("设为直连", hint)
        self.assertIn("导入根证书也无法修复", hint)


class TestCustomerSiteBoundaries(unittest.TestCase):
    def test_entrypoint_only_toggles_www(self):
        self.assertEqual(
            crawl.entrypoint_candidates("https://www.example.com"),
            ["https://www.example.com", "https://example.com"],
        )
        self.assertEqual(
            crawl.entrypoint_candidates("https://example.com"),
            ["https://example.com", "https://www.example.com"],
        )

    def test_filter_robots_respects_customer_rules(self):
        urls = ["https://x.test/", "https://x.test/public", "https://x.test/private/a"]
        allowed, skipped = crawl.filter_robots(
            urls, "User-agent: *\nDisallow: /private\nAllow: /public\n",
        )
        self.assertEqual(allowed, urls[:2])
        self.assertEqual(skipped[0]["url"], urls[2])
        self.assertIn("Disallow", skipped[0]["rule"])

    def test_discover_sitemap_does_not_refetch_supplied_robots(self):
        robots = "Sitemap: https://x.test/custom.xml\n"

        def fake_fetch(url, timeout=8):
            if url.endswith("custom.xml"):
                return "<urlset><url><loc>https://x.test/a</loc></url></urlset>"
            return ""

        with mock.patch.object(crawl.G, "fetch_text", side_effect=fake_fetch) as fetch:
            urls = crawl.discover_sitemap("https://x.test", robots_txt=robots)
        self.assertEqual(urls, ["https://x.test/a"])
        self.assertNotIn("https://x.test/robots.txt", [c.args[0] for c in fetch.call_args_list])


class TestWordCountKana(unittest.TestCase):
    def test_pure_kana_counts(self):
        self.assertGreater(G.word_count("これはテストです"), 0)

    def test_cjk_unchanged(self):
        self.assertGreater(G.word_count("这是一个测试"), 0)


if __name__ == "__main__":
    unittest.main()

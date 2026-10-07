import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard as D
import access as AC


class TestAuthOk(unittest.TestCase):
    TOKEN = "s3cret-token"

    def test_no_token_configured_allows_all(self):
        self.assertTrue(D.auth_ok(None, None))
        self.assertTrue(D.auth_ok("", "anything=1"))

    def test_query_token_matches(self):
        self.assertTrue(D.auth_ok(self.TOKEN, None, query_token=self.TOKEN))

    def test_header_token_matches(self):
        self.assertTrue(D.auth_ok(self.TOKEN, None, header_token=self.TOKEN))

    def test_cookie_digest_matches(self):
        cookie = f"other=1; {D.AUTH_COOKIE}={D._token_digest(self.TOKEN)}"
        self.assertTrue(D.auth_ok(self.TOKEN, cookie))

    def test_wrong_credentials_rejected(self):
        self.assertFalse(D.auth_ok(self.TOKEN, None))
        self.assertFalse(D.auth_ok(self.TOKEN, None, query_token="wrong"))
        self.assertFalse(D.auth_ok(self.TOKEN, f"{D.AUTH_COOKIE}=deadbeef"))
        # cookie 里放原始令牌不行——cookie 存的是摘要
        self.assertFalse(D.auth_ok(self.TOKEN, f"{D.AUTH_COOKIE}={self.TOKEN}"))


class TestUsers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(D.os.environ, {
            "GEOLOOK_USERS_FILE": str(Path(self.tmp.name) / "users.json")
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_create_and_authenticate_user(self):
        made = D.create_user("admin", "correct-horse", "admin")
        self.assertEqual(made["username"], "admin")
        self.assertNotIn("password_hash", made)
        self.assertEqual(D.authenticate_user("ADMIN", "correct-horse")["role"], "admin")
        self.assertIsNone(D.authenticate_user("admin", "wrong-password"))
        raw = json.loads(Path(D.os.environ["GEOLOOK_USERS_FILE"]).read_text("utf-8"))
        self.assertNotIn("correct-horse", json.dumps(raw))

    def test_duplicate_username_is_case_insensitive(self):
        D.create_user("Alice", "long-password", "user")
        with self.assertRaisesRegex(ValueError, "已存在"):
            D.create_user("alice", "other-password", "user")

    def test_last_admin_cannot_be_disabled(self):
        D.create_user("admin", "long-password", "admin")
        with self.assertRaisesRegex(ValueError, "最后一个管理员"):
            D.update_user("admin", active=False)

    def test_user_can_be_reset_and_disabled(self):
        D.create_user("admin", "long-password", "admin")
        D.create_user("member", "first-password", "user")
        D.update_user("member", password="second-password", active=False)
        self.assertIsNone(D.authenticate_user("member", "second-password"))
        D.update_user("member", active=True)
        self.assertIsNotNone(D.authenticate_user("member", "second-password"))


class TestPublicBindGuard(unittest.TestCase):
    def test_public_host_without_token_dies(self):
        with mock.patch.dict(D.os.environ, {}, clear=True), \
             self.assertRaises(SystemExit):
            D.run(port=0, open_browser=False, host="0.0.0.0", token=None)


class TestHostedHealthCheck(unittest.TestCase):
    def test_healthz_bypasses_auth_and_has_security_headers(self):
        D.Handler.TOKEN = "secret"
        D.Handler.COOKIE_SECURE = False
        srv = D.ThreadingHTTPServer(("127.0.0.1", 0), D.Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/healthz"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data, {"ok": True, "service": "geolook"})
                self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        finally:
            srv.shutdown()
            srv.server_close()
            D.Handler.TOKEN = None


class TestEnvBool(unittest.TestCase):
    def test_env_bool(self):
        with mock.patch.dict(D.os.environ, {"FLAG": "yes"}, clear=True):
            self.assertTrue(D._env_bool("FLAG"))
        with mock.patch.dict(D.os.environ, {"FLAG": "0"}, clear=True):
            self.assertFalse(D._env_bool("FLAG", True))
        with mock.patch.dict(D.os.environ, {}, clear=True):
            self.assertTrue(D._env_bool("FLAG", True))


class TestProjectAccessHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = mock.patch.object(D.G, "WORK", Path(self.tmp.name) / "work")
        self.work.start()
        self.addCleanup(self.work.stop)
        self.users = mock.patch.dict(D.os.environ, {
            "GEOLOOK_USERS_FILE": str(Path(self.tmp.name) / "users.json")})
        self.users.start()
        self.addCleanup(self.users.stop)
        D._SESSIONS.clear()
        D.create_user("root", "long-password", "admin")
        D.create_user("alice", "long-password", "user")
        D.create_user("bob", "long-password", "user")
        for slug in ("alpha", "beta"):
            D.G.write_json(D.G.project_dir(slug) / "geo.json",
                           {"slug": slug, "brand": {"name": slug}, "questions": []})
        AC.set_member("alpha", "alice", "editor")
        AC.set_member("beta", "bob", "viewer")
        D.Handler.TOKEN = None
        self.server = D.ThreadingHTTPServer(("127.0.0.1", 0), D.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, username, path, body=None):
        sid = D._new_session({"username": username, "role": "admin" if username == "root" else "user",
                              "active": True})
        raw = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}{path}",
                                     data=raw, headers={"Cookie": f"{D.SESSION_COOKIE}={sid}",
                                                        "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_membership_filters_projects_and_global_key_changes(self):
        status, projects = self.request("alice", "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual([p["slug"] for p in projects], ["alpha"])
        self.assertEqual(self.request("alice", "/api/config/beta")[0], 403)
        self.assertEqual(self.request("bob", "/api/config/alpha")[0], 403)
        self.assertEqual(self.request("alice", "/api/keys", {"updates": {"ARK_API_KEY": "x"}})[0], 403)
        self.assertEqual(self.request("alice", "/api/project-members/alpha")[0], 403)
        self.assertEqual(self.request("bob", "/api/config/beta", {"notes": "changed"})[0], 403)
        self.assertEqual(self.request("root", "/api/projects")[0], 200)

    def test_config_version_rejects_stale_edit(self):
        _, cfg = self.request("alice", "/api/config/alpha")
        self.assertEqual(self.request("alice", "/api/config/alpha",
                                      {"notes": "first", "_revision": cfg["_revision"]})[0], 200)
        status, result = self.request("alice", "/api/config/alpha",
                                      {"notes": "stale", "_revision": cfg["_revision"]})
        self.assertEqual(status, 409)
        self.assertIn("其他人修改", result["error"])
        self.assertEqual(D.G.load_config("alpha")["notes"], "first")

    def test_config_edit_is_allowed_during_staged_job(self):
        _, cfg = self.request("alice", "/api/config/alpha")
        with mock.patch.object(D.G, "acquire_run_lock", return_value=None):
            status, _ = self.request("alice", "/api/config/alpha",
                                     {"notes": "next cycle", "_revision": cfg["_revision"]})
        self.assertEqual(status, 200)
        self.assertEqual(D.G.load_config("alpha")["notes"], "next cycle")

    def test_file_route_cannot_escape_authorized_project(self):
        sid = D._new_session({"username": "alice", "role": "user", "active": True})
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/files/alpha/%2e%2e/beta/geo.json",
            headers={"Cookie": f"{D.SESSION_COOKIE}={sid}"})
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(denied.exception.code, 403)

    def test_second_run_returns_existing_job_and_viewer_cannot_start(self):
        active = {"id": "abcdef123456", "slug": "alpha", "label": "更新本期数据",
                  "started_by": "root", "status": "running"}
        with mock.patch.object(D.J, "start", side_effect=D.J.JobBusyError(active)):
            status, result = self.request("alice", "/api/run",
                                          {"slug": "alpha", "action": "serve"})
        self.assertEqual(status, 409)
        self.assertEqual(result["job"]["id"], active["id"])
        self.assertEqual(self.request("bob", "/api/run",
                                      {"slug": "beta", "action": "serve"})[0], 403)


if __name__ == "__main__":
    unittest.main()

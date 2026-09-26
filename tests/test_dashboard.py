import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard as D


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


if __name__ == "__main__":
    unittest.main()

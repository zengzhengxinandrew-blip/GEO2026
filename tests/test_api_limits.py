import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import api_limits as L
import geolib as G


class ApiLimitsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = mock.patch.object(G, "ENV_PATH", Path(self.tmp.name) / "missing.env")
        self.env_file.start()
        self.env = mock.patch.dict(os.environ, {
            "GEOLOOK_API_LIMITS_DIR": self.tmp.name,
            "GEOLOOK_API_DAILY_CALL_LIMIT": "2",
            "GEOLOOK_API_MAX_CONCURRENCY": "1",
            "GEOLOOK_API_DAILY_BUDGET_CNY": "",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.env_file.stop()
        self.tmp.cleanup()

    def test_daily_call_limit_is_shared_across_providers(self):
        with L.reserve("kimi"):
            pass
        with L.reserve("doubao"):
            pass
        with self.assertRaisesRegex(L.LimitReached, "每日 API 调用上限"):
            with L.reserve("deepseek"):
                pass

    def test_money_cap_requires_explicit_worst_case_price(self):
        with mock.patch.dict(os.environ, {"GEOLOOK_API_DAILY_BUDGET_CNY": "1"}):
            with self.assertRaisesRegex(L.LimitReached, "未设置"):
                with L.reserve("kimi"):
                    pass
            with mock.patch.dict(os.environ, {
                "GEOLOOK_API_MAX_ATTEMPT_COST_CNY_KIMI": "0.2"}):
                with L.reserve("kimi"):
                    pass
                with self.assertRaisesRegex(L.LimitReached, "费用预留上限"):
                    with L.reserve("kimi"):
                        pass

    def test_corrupt_ledger_fails_closed(self):
        path = Path(self.tmp.name) / f"{date.today().isoformat()}.json"
        path.write_text("{broken", "utf-8")
        with self.assertRaisesRegex(L.LimitReached, "用量记录损坏"):
            with L.reserve("kimi"):
                pass


if __name__ == "__main__":
    unittest.main()

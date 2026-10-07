"""模型配置链路的回归测试。

守住三件事：
1. 模型在调用时解析——界面改完立即生效，清掉覆盖回落出厂默认（曾经在 import 时固化）；
2. bootstrap/expand/generate 共用一条 LLM 候选链，不各自漂移；
3. write_env 写盘与进程环境同步；清除标记持久化，重启不恢复旧凭据。
"""
import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import dashboard as DB
import sample as S

ALL_KEY_ENVS = [p["key_env"] for p in S.PROVIDERS.values()]
ALL_MODEL_ENVS = [p["model_env"] for p in S.PROVIDERS.values() if p.get("model_env")]
_CLEAR = {k: "" for k in ALL_KEY_ENVS + ALL_MODEL_ENVS}


def no_llm_env():
    """清空所有 Key/模型环境变量的上下文。"""
    env = {k: v for k, v in os.environ.items() if k not in _CLEAR}
    return mock.patch.dict(os.environ, env, clear=True)


class TestModelResolution(unittest.TestCase):
    def test_registry_stores_pure_defaults(self):
        # 注册表里必须是出厂默认，不能在 import 时把环境变量固化进去
        for code, p in S.PROVIDERS.items():
            menv = p.get("model_env")
            if menv and os.environ.get(menv):
                self.assertNotEqual(
                    p["model"], os.environ[menv],
                    f"{code}: 注册表 model 被环境变量固化，会导致清除覆盖后无法回落默认")

    def test_model_for_env_override_and_fallback(self):
        default = S.PROVIDERS["glm"]["model"]
        with mock.patch.dict(os.environ, {"GLM_MODEL": "glm-x-test"}):
            self.assertEqual(S.model_for("glm"), "glm-x-test")
        with no_llm_env():
            self.assertEqual(S.model_for("glm"), default)

    def test_ask_uses_call_time_model(self):
        captured = {}

        def fake_post(url, **kw):
            captured["model"] = kw["json"]["model"]

            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {"choices": [{"message": {"content": "ok"}}]}
            return R()

        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "k",
                                          "DEEPSEEK_MODEL": "ds-live-override"}):
            with mock.patch.object(S.requests, "post", side_effect=fake_post):
                res = S.ask("deepseek", "hi", timeout=5)
        self.assertTrue(res["ok"])
        self.assertEqual(captured["model"], "ds-live-override")

    def test_ask_without_key_fails_gracefully(self):
        with no_llm_env():
            res = S.ask("deepseek", "hi")
        self.assertFalse(res["ok"])
        self.assertIn("DEEPSEEK_API_KEY", res["error"])


class TestPickLLM(unittest.TestCase):
    def test_none_when_no_keys(self):
        with no_llm_env():
            self.assertIsNone(S.pick_llm())

    def test_chain_order_and_prefer(self):
        with no_llm_env():
            with mock.patch.dict(os.environ, {"ZHIPUAI_API_KEY": "k",
                                              "OPENAI_API_KEY": "k"}):
                self.assertEqual(S.pick_llm(), "glm")          # 链上第一个可用
                self.assertEqual(S.pick_llm("openai"), "openai")  # 指定优先
                self.assertIsNone(S.pick_llm("claude"))        # 指定但没配 → None，不偷换

    def test_consumers_share_the_chain(self):
        # bootstrap / expand / generate 都必须走 pick_llm，不允许再各写一份候选链
        import inspect

        import bootstrap
        import expand
        import generate
        for modname, fn in (("bootstrap", bootstrap._ask_json),
                            ("expand", expand._convert_llm),
                            ("generate", generate.draft)):
            src = inspect.getsource(fn)
            self.assertIn("pick_llm", src, f"{modname} 未使用统一候选链 pick_llm")


class TestWriteEnv(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX file mode assertion")
    def test_roundtrip_set_and_delete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(DB.G, "ROOT", root), \
                 mock.patch.object(DB.G, "ENV_PATH", root / ".env"):
                with mock.patch.dict(os.environ, {}, clear=False):
                    DB.write_env({"GLM_MODEL": "glm-test-1"})
                    self.assertEqual(os.environ.get("GLM_MODEL"), "glm-test-1")
                    text = (root / ".env").read_text()
                    self.assertIn("GLM_MODEL=glm-test-1", text)
                    self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
                    # 与调用链联动：写完 model_for 立即取到新值
                    self.assertEqual(S.model_for("glm"), "glm-test-1")
                    # 空值持久化：阻止容器旧配置在重启后复活，模型回落默认。
                    DB.write_env({"GLM_MODEL": ""})
                    self.assertIn("GLM_MODEL=\n", (root / ".env").read_text())
                    self.assertEqual(os.environ.get("GLM_MODEL"), "")
                    self.assertEqual(S.model_for("glm"), S.PROVIDERS["glm"]["model"])

    @unittest.skipIf(os.name == "nt", "Linux child process loads fcntl")
    def test_restart_uses_saved_values_and_clear_markers(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ):
            path = Path(td) / ".env"
            with mock.patch.object(DB.G, "ENV_PATH", path):
                DB.write_env({"ARK_API_KEY": "synthetic-new", "MOONSHOT_API_KEY": "synthetic-kimi",
                              "ARK_MODEL": "synthetic-model"})
                self.assertEqual(self._restart(path), ["synthetic-new", "synthetic-kimi", "synthetic-model"])
                DB.write_env({"ARK_API_KEY": "", "MOONSHOT_API_KEY": "", "ARK_MODEL": ""})
                self.assertEqual(self._restart(path), ["", "", ""])
                DB.write_env({"ARK_API_KEY": "synthetic-next"})
                self.assertEqual(self._restart(path), ["synthetic-next", "", ""])

    @staticmethod
    def _restart(path):
        env = dict(os.environ, GEOLOOK_ENV_FILE=str(path), ARK_API_KEY="synthetic-old",
                   MOONSHOT_API_KEY="synthetic-old", ARK_MODEL="synthetic-old")
        code = ("import geolib, os, json; print(json.dumps([os.environ.get(k) for k in "
                "('ARK_API_KEY', 'MOONSHOT_API_KEY', 'ARK_MODEL')]))")
        out = subprocess.check_output([sys.executable, "-c", code], env=env,
                                      cwd=Path(S.__file__).parent, text=True)
        return json.loads(out)

    def test_failed_save_keeps_file_and_memory(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"ARK_API_KEY": "old"}):
            path = Path(td) / ".env"
            path.write_text("ARK_API_KEY=old\n", encoding="utf-8")
            with mock.patch.object(DB.G, "ENV_PATH", path), \
                 mock.patch.object(DB.os, "replace", side_effect=OSError("simulated write failure")):
                with self.assertRaises(OSError):
                    DB.write_env({"ARK_API_KEY": "new"})
            self.assertEqual(path.read_text(), "ARK_API_KEY=old\n")
            self.assertEqual(os.environ["ARK_API_KEY"], "old")
            self.assertEqual(list(Path(td).iterdir()), [path])


class TestEnvPrecedence(unittest.TestCase):
    def test_allowlist_matches_editable_registry(self):
        import publish as P
        expected = set(ALL_KEY_ENVS + ALL_MODEL_ENVS)
        for spec in P.PUBLISHERS.values():
            expected.update(spec["env"])
        expected.update({"GEOLOOK_API_MAX_CONCURRENCY", "GEOLOOK_API_DAILY_CALL_LIMIT",
                         "GEOLOOK_API_DAILY_BUDGET_CNY"})
        expected.update(f"GEOLOOK_API_MAX_ATTEMPT_COST_CNY_{code.upper()}"
                        for code in S.PROVIDERS)
        self.assertEqual(DB.G.UI_ENV_KEYS, expected)

    def test_legacy_file_export_and_deployment_settings(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {
            "ARK_API_KEY": "old", "MOONSHOT_API_KEY": "inherited",
            "GEOLOOK_TOKEN": "deployment-token", "GEOLOOK_WORK_DIR": "/deployment/work",
            "GEOLOOK_PORT": "8765", "OPENAI_BASE_URL": "https://deployment.invalid",
        }, clear=True):
            path = Path(td) / ".env"
            path.write_text('export ARK_API_KEY="saved"\nGEOLOOK_TOKEN=file-token\n'
                            'GEOLOOK_WORK_DIR=/wrong/work\nGEOLOOK_PORT=9999\n'
                            'OPENAI_BASE_URL=https://file.invalid\n', encoding="utf-8")
            with mock.patch.object(DB.G, "ENV_PATH", path):
                DB.G.load_env()
            self.assertEqual(os.environ["ARK_API_KEY"], "saved")
            self.assertEqual(os.environ["MOONSHOT_API_KEY"], "inherited")
            self.assertEqual(os.environ["GEOLOOK_TOKEN"], "deployment-token")
            self.assertEqual(os.environ["GEOLOOK_WORK_DIR"], "/deployment/work")
            self.assertEqual(os.environ["GEOLOOK_PORT"], "8765")
            self.assertEqual(os.environ["OPENAI_BASE_URL"], "https://deployment.invalid")

    def test_empty_environment_and_missing_file(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {"ARK_API_KEY": ""}, clear=True):
            path = Path(td) / ".env"
            DB.G.load_env(path)
            self.assertEqual(os.environ["ARK_API_KEY"], "")
            path.write_text("ARK_API_KEY=saved\n", encoding="utf-8")
            DB.G.load_env(path)
            self.assertEqual(os.environ["ARK_API_KEY"], "saved")


if __name__ == "__main__":
    unittest.main()

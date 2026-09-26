import re
import unittest
from pathlib import Path

UI = Path(__file__).parent.parent / "scripts" / "ui.html"


class DocumentLangCase(unittest.TestCase):
    """回归：界面语言切换必须同步 <html lang>（WCAG 3.1.1，issue #1）。"""

    def setUp(self):
        self.html = UI.read_text("utf-8")

    def test_default_lang_is_zh_cn(self):
        self.assertIn('<html lang="zh-CN">', self.html)

    def test_ulang_updates_document_lang(self):
        m = re.search(
            r"document\.documentElement\.lang\s*=\s*(\{[^}]*\})\[ULANG\]", self.html
        )
        self.assertIsNotNone(m, "ULANG 未同步到 document.documentElement.lang")
        mapping = m.group(1)
        self.assertIn("zh:'zh-CN'", mapping)
        self.assertIn("en:'en'", mapping)
        self.assertNotIn("ja:'ja'", mapping)

    def test_language_switch_only_exposes_chinese_and_english(self):
        self.assertIn("[['zh','中'],['en','EN']]", self.html)
        self.assertNotIn("[['zh','中'],['en','EN'],['ja','日']]", self.html)
        self.assertIn("class=\"wordmark\"", self.html)

    def test_brand_and_user_management_are_present(self):
        self.assertIn("Get Found By AI", self.html)
        self.assertIn("['users','用户管理',true]", self.html)

    def test_configured_engines_use_success_state(self):
        self.assertIn(".engine-key-row.configured", self.html)
        self.assertIn("'var(--success)'", self.html)


if __name__ == "__main__":
    unittest.main()

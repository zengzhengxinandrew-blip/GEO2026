import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import geo


class InitialProjectLockTest(unittest.TestCase):
    def test_derived_slug_matches_init(self):
        args = SimpleNamespace(slug=None, url="https://www.example.com/path", name=None,
                               no_site=False)
        self.assertEqual(geo._initial_slug(args), "example")

    def test_new_command_locks_derived_slug(self):
        with mock.patch.object(sys, "argv", ["geo", "new", "--url", "https://www.example.com"]), \
             mock.patch.object(geo.G, "acquire_run_lock") as acquire, \
             mock.patch.object(geo, "cmd_new") as run:
            acquire.return_value.__enter__ = mock.Mock()
            acquire.return_value.__exit__ = mock.Mock(return_value=False)
            geo.main()
        acquire.assert_called_once_with("example")
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()

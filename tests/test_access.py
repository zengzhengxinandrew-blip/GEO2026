import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import geolib as G
import access as A


class ProjectAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = mock.patch.object(G, "WORK", Path(self.tmp.name))
        self.work.start()

    def tearDown(self):
        self.work.stop()
        self.tmp.cleanup()

    def test_existing_projects_are_admin_only_until_assigned(self):
        user = {"username": "Alice", "role": "user", "active": True}
        admin = {"username": "root", "role": "admin", "active": True}
        self.assertFalse(A.allowed(user, "client"))
        self.assertTrue(A.allowed(admin, "client", "admin"))
        A.set_member("client", "Alice", "viewer")
        self.assertTrue(A.allowed(user, "client"))
        self.assertFalse(A.allowed(user, "client", "editor"))
        A.set_member("client", "alice", "editor")
        self.assertTrue(A.allowed(user, "client", "editor"))
        self.assertFalse(A.allowed(user, "other"))

    def test_deleted_user_loses_project_memberships(self):
        A.set_member("client", "Alice", "admin")
        A.remove_user("alice")
        self.assertEqual(A.members("client"), [])


if __name__ == "__main__":
    unittest.main()

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import jobs as J
import geolib as G


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_dir = J.JOBS_DIR
        J.JOBS_DIR = Path(self.tmp.name) / ".jobs"
        self.addCleanup(setattr, J, "JOBS_DIR", self._orig_dir)
        self._orig_work = G.WORK
        G.WORK = Path(self.tmp.name) / "work"
        self.addCleanup(setattr, G, "WORK", self._orig_work)
        J._running.clear()
        J._procs.clear()
        self.addCleanup(J._running.clear)
        self.addCleanup(J._procs.clear)

    def _write_job(self, job_id, **kw):
        job = {"id": job_id, "slug": "x", "action": "audit", "label": "页面体检",
               "status": "running", "started_at": "2026-07-28T10:00:00",
               "finished_at": None, "exit_code": None}
        job.update(kw)
        J.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        (J.JOBS_DIR / f"{job_id}.json").write_text(json.dumps(job), "utf-8")
        return job

    def test_reap_orphans_dead_pid(self):
        self._write_job("deadjob12345", pid=999999)
        n = J.reap_orphans()
        self.assertEqual(n, 1)
        j = J.get("deadjob12345")
        self.assertEqual(j["status"], "interrupted")
        self.assertTrue(j["finished_at"])

    def test_reap_orphans_released_lock_even_if_pid_reused(self):
        self._write_job("livejob12345", pid=os.getpid())
        n = J.reap_orphans()
        self.assertEqual(n, 1)
        self.assertEqual(J.get("livejob12345")["status"], "interrupted")

    def test_reap_orphans_keeps_held_lock(self):
        self._write_job("livejob12345", pid=os.getpid())
        with mock.patch.object(G, "run_locked", return_value=True):
            self.assertEqual(J.reap_orphans(), 0)
        self.assertEqual(J.get("livejob12345")["status"], "running")

    def test_reap_orphans_skips_non_running(self):
        self._write_job("donejob123456", status="done", pid=999999)
        with mock.patch.object(J.os, "kill", side_effect=ProcessLookupError):
            n = J.reap_orphans()
        self.assertEqual(n, 0)
        self.assertEqual(J.get("donejob123456")["status"], "done")

    def test_start_popen_failure_marks_failed(self):
        with mock.patch.object(J.subprocess, "Popen", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                J.start("x", "audit")
        jobs = list(J.JOBS_DIR.glob("*.json"))
        self.assertEqual(len(jobs), 1)
        j = json.loads(jobs[0].read_text("utf-8"))
        self.assertEqual(j["status"], "failed")
        self.assertIn("boom", j["error"])
        self.assertTrue(j["finished_at"])
        self.assertNotIn(j["id"], J._procs)
        self.assertNotIn("x", J._running)

    def test_start_writes_pid(self):
        proc = mock.Mock()
        proc.pid = 424242
        proc.wait.return_value = 0
        # 此测试只检查 PID 落盘，同步结束 waiter，避免临时目录先被清理。
        with mock.patch.object(J.subprocess, "Popen", return_value=proc), \
             mock.patch.object(J.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            job = J.start("x", "audit")
        j = J.get(job["id"])
        self.assertEqual(j["pid"], 424242)

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_stop_fallback_by_pid(self):
        self._write_job("orphan1234567", pid=31337)
        with mock.patch.object(G, "run_locked", return_value=True), \
             mock.patch.object(J, "running_for", return_value="orphan1234567"), \
             mock.patch.object(J.os, "getpgid", return_value=31337) as g, \
             mock.patch.object(J.os, "killpg") as k:
            ok = J.stop("orphan1234567")
        self.assertTrue(ok)
        g.assert_called_once_with(31337)
        k.assert_called_once()
        j = J.get("orphan1234567")
        self.assertEqual(j["status"], "stopping")

    def test_reap_skips_young_job_without_pid(self):
        self._write_job("youngjob12345")  # 刚落盘、还没来得及补 pid
        with mock.patch.object(G, "run_locked", return_value=True):
            self.assertEqual(J.reap_orphans(), 0)
        self.assertEqual(J.get("youngjob12345")["status"], "running")

    def test_reap_old_job_without_pid(self):
        p = J.JOBS_DIR / "oldjob1234567.json"
        self._write_job("oldjob1234567")
        old = 1700000000
        os.utime(p, (old, old))
        self.assertEqual(J.reap_orphans(), 1)
        self.assertEqual(J.get("oldjob1234567")["status"], "interrupted")

    def test_stop_unknown_job(self):
        self.assertFalse(J.stop("nosuchjob000"))

    def test_get_corrupt_json_returns_none(self):
        J.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        (J.JOBS_DIR / "badjob123456.json").write_text("{not json", "utf-8")
        self.assertIsNone(J.get("badjob123456"))

    def test_long_pipelines_allow_skip_crawl(self):
        self.assertIn("--skip-crawl", J.ACTIONS["autopilot"]["args"])
        self.assertIn("--skip-crawl", J.ACTIONS["serve"]["args"])
        self.assertIn("--skip-crawl", J.FLAG_ARGS)

    @unittest.skipIf(os.name == "nt", "Windows test shim cannot verify POSIX flock")
    def test_project_run_lock_is_atomic(self):
        first = G.acquire_run_lock("x")
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(G.acquire_run_lock("x"))
        finally:
            first.close()
        second = G.acquire_run_lock("x")
        self.assertIsNotNone(second)
        second.close()

    def test_successful_job_publishes_staged_files(self):
        live = G.project_dir("x")
        live.mkdir(parents=True)
        G.write_json(live / "geo.json", {"slug": "x"})
        (live / "marker.txt").write_text("old", "utf-8")

        def spawn(_cmd, **kw):
            (Path(kw["env"]["GEOLOOK_WORK_DIR"]) / "x" / "marker.txt").write_text("new", "utf-8")
            proc = mock.Mock()
            proc.pid = 4242
            proc.wait.return_value = 0
            return proc

        with mock.patch.object(J.subprocess, "Popen", side_effect=spawn), \
             mock.patch.object(J.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            job = J.start("x", "audit")
        self.assertEqual((live / "marker.txt").read_text("utf-8"), "new")
        self.assertEqual(J.get(job["id"])["status"], "done")

    def test_failed_job_keeps_previous_result(self):
        live = G.project_dir("x")
        live.mkdir(parents=True)
        (live / "marker.txt").write_text("old", "utf-8")

        def spawn(_cmd, **kw):
            (Path(kw["env"]["GEOLOOK_WORK_DIR"]) / "x" / "marker.txt").write_text("new", "utf-8")
            proc = mock.Mock()
            proc.pid = 4242
            proc.wait.return_value = 1
            return proc

        with mock.patch.object(J.subprocess, "Popen", side_effect=spawn), \
             mock.patch.object(J.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            job = J.start("x", "audit")
        self.assertEqual((live / "marker.txt").read_text("utf-8"), "old")
        self.assertEqual(J.get(job["id"])["status"], "failed")
        self.assertTrue((J.JOBS_DIR / "staging" / job["id"] / "x" / "marker.txt").exists())

    def test_schedule_advance_is_only_published_after_success(self):
        live = G.project_dir("x")
        live.mkdir(parents=True)
        G.write_json(live / "geo.json", {"slug": "x", "monitor": {"next_run": "2026-10-07"}})

        def spawn(_cmd, **kw):
            staged = G.read_json(Path(kw["env"]["GEOLOOK_WORK_DIR"]) / "x" / "geo.json")
            self.assertEqual(staged["monitor"]["next_run"], "2026-10-14")
            proc = mock.Mock(pid=4242)
            proc.wait.return_value = 1
            return proc

        with mock.patch.object(J.subprocess, "Popen", side_effect=spawn), \
             mock.patch.object(J.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            J.start("x", "serve", scheduled_next_run="2026-10-14")
        self.assertEqual(G.load_config("x")["monitor"]["next_run"], "2026-10-07")

    def test_publish_failure_rolls_back_replaced_files(self):
        live = G.project_dir("x")
        staged = J.JOBS_DIR / "staging" / "job" / "x"
        live.mkdir(parents=True)
        staged.mkdir(parents=True)
        for name in ("a.txt", "b.txt"):
            (live / name).write_text("old", "utf-8")
            (staged / name).write_text("new", "utf-8")
        original_replace = J.os.replace

        def fail_second(src, dst):
            if Path(dst) == live / "b.txt":
                raise OSError("disk full")
            return original_replace(src, dst)

        with mock.patch.object(J.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "disk full"):
                J._publish_stage("x", staged.parent)
        self.assertEqual((live / "a.txt").read_text("utf-8"), "old")
        self.assertEqual((live / "b.txt").read_text("utf-8"), "old")

    def test_running_edit_survives_publish_and_disjoint_generated_config_is_kept(self):
        live = G.project_dir("x")
        staged = J.JOBS_DIR / "staging" / "job" / "x"
        live.mkdir(parents=True)
        staged.mkdir(parents=True)
        base = {"brand": {"name": "old"}, "monitor": {"next_run": "2026-10-07"}}
        G.write_json(live / "geo.json", {"brand": {"name": "edited"},
                                          "monitor": {"next_run": "2026-10-07"}})
        G.write_json(staged / "geo.json", {"brand": {"name": "generated"},
                                             "monitor": {"next_run": "2026-10-14"}})
        _, conflicts = J._publish_stage("x", staged.parent, base)
        self.assertEqual(G.load_config("x"), {"brand": {"name": "edited"},
                                               "monitor": {"next_run": "2026-10-14"}})
        self.assertEqual(conflicts, ["brand.name"])

    def test_old_failed_stage_is_pruned_but_running_stage_is_retained(self):
        old_id, live_id = "abcdef123456", "123456abcdef"
        for jid, status in ((old_id, "failed"), (live_id, "running")):
            path = J.JOBS_DIR / "staging" / jid / "x"
            path.mkdir(parents=True)
            (path / "output.txt").write_text("copy", "utf-8")
            self._write_job(jid, status=status)
            os.utime(J.JOBS_DIR / f"{jid}.json", (1700000000, 1700000000))
        self.assertEqual(J.prune_failed_stages(), 1)
        self.assertFalse((J.JOBS_DIR / "staging" / old_id).exists())
        self.assertTrue((J.JOBS_DIR / "staging" / live_id).exists())


if __name__ == "__main__":
    unittest.main()

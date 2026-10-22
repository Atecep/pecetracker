"""Instance ownership tests never send real termination signals."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for p in (patch.object(launcher, "INSTANCE_LOCK_PATH", self.root / "instance.lock"),
                  patch.object(launcher, "INSTANCE_STATE_PATH", self.root / "instance.json"),
                  patch.object(launcher, "ensure_data_dir")):
            p.start(); self.addCleanup(p.stop)
        self.addCleanup(launcher.release_instance_lock)

    def test_current_identity_and_pid_reuse(self):
        identity = launcher.process_identity(os.getpid())
        self.assertTrue(launcher.identity_matches(os.getpid(), identity))
        self.assertFalse(launcher.identity_matches(os.getpid(), {**identity, "created_at": identity["created_at"] - 1}))
        self.assertFalse(launcher.identity_matches(os.getpid(), None))

    def test_stale_identity_never_opens_a_termination_handle(self):
        with patch.object(launcher, "identity_matches", return_value=False), patch.object(launcher.os, "pidfd_open", create=True) as pidfd:
            with self.assertRaises(RuntimeError): launcher.terminate_process(12345, {"created_at": 1})
            pidfd.assert_not_called()

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux stable process handles")
    def test_pid_reuse_between_validation_and_handle_open_never_signals(self):
        with patch.object(launcher, "identity_matches", side_effect=[True, False]), patch.object(launcher.os, "pidfd_open", return_value=77), patch.object(launcher.os, "close") as close, patch.object(launcher.signal, "pidfd_send_signal") as send:
            with self.assertRaises(RuntimeError): launcher.terminate_process(12345, {"created_at": 1})
            send.assert_not_called(); close.assert_called_once_with(77)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Linux stable process handles")
    def test_verified_process_is_signalled_only_through_its_handle(self):
        with patch.object(launcher, "identity_matches", return_value=True), patch.object(launcher.os, "pidfd_open", return_value=77), patch.object(launcher.os, "close"), patch.object(launcher.signal, "pidfd_send_signal") as send:
            launcher.terminate_process(12345, {"created_at": 1})
            self.assertEqual(send.call_args.args, (77, launcher.signal.SIGTERM))

    def test_legacy_instance_cannot_be_terminated(self):
        launcher.INSTANCE_STATE_PATH.write_text(json.dumps({"pid": 12345, "port": 8501, "version": "0.1.0"}))
        with patch.object(launcher, "pid_is_running", return_value=True), patch.object(launcher.os, "kill") as kill:
            with self.assertRaises(RuntimeError): launcher.reopen_existing_instance()
            kill.assert_not_called()

    def test_same_version_legacy_launcher_is_not_silently_reopened(self):
        launcher.INSTANCE_STATE_PATH.write_text(json.dumps({'pid':12345,'port':8501,'version':launcher.APP_VERSION}))
        with patch.object(launcher,'pid_is_running',return_value=True),patch.object(launcher,'server_is_healthy',return_value=True),patch.object(launcher.webbrowser,'open') as open_browser:
            with self.assertRaisesRegex(RuntimeError,'legacy'):launcher.reopen_existing_instance()
            open_browser.assert_not_called()

    def test_locked_file_cannot_be_reclaimed_even_during_partial_json(self):
        # A child holds the actual OS lock BEFORE writing its JSON. Parent must
        # not unlink it or acquire a second lock; works across processes on CI.
        code = """
import launcher, sys
from pathlib import Path
launcher.INSTANCE_LOCK_PATH=Path(sys.argv[1])
f=open(launcher.INSTANCE_LOCK_PATH,'w+b')
launcher._lock_file(f)
print('held',flush=True)
sys.stdin.readline()
f.close()
"""
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(launcher.INSTANCE_LOCK_PATH)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            with patch.object(launcher, "reopen_existing_instance", return_value=False):
                with self.assertRaises(RuntimeError): launcher.acquire_instance_lock()
            self.assertTrue(launcher.INSTANCE_LOCK_PATH.exists())
        finally:
            child.communicate("release\n", timeout=10)
        self.assertEqual(child.returncode, 0)
        self.assertEqual(launcher.acquire_instance_lock(), os.getpid())

    def test_release_keeps_same_inode_and_allows_next_owner(self):
        launcher.acquire_instance_lock()
        first = launcher.INSTANCE_LOCK_PATH.stat().st_ino
        launcher.release_instance_lock()
        launcher.acquire_instance_lock()
        self.assertEqual(launcher.INSTANCE_LOCK_PATH.stat().st_ino, first)

    def test_failed_startup_releases_lock(self):
        with patch.object(launcher, "configure_logging"), patch.object(launcher, "reopen_existing_instance", return_value=False), patch.object(launcher, "find_free_port", side_effect=RuntimeError("fixture busy")):
            with self.assertRaises(RuntimeError): launcher.main()
        self.assertIsNone(launcher._instance_lock_file)
        self.assertEqual(launcher.acquire_instance_lock(), os.getpid())

    def test_state_cleanup_requires_own_token(self):
        launcher.acquire_instance_lock();launcher.write_instance_state(os.getpid(),8501)
        state=json.loads(launcher.INSTANCE_STATE_PATH.read_text())
        state['instance_token']='another owner'
        launcher.INSTANCE_STATE_PATH.write_text(json.dumps(state))
        launcher.remove_if_owned(launcher.INSTANCE_STATE_PATH,os.getpid())
        self.assertTrue(launcher.INSTANCE_STATE_PATH.exists())

    def test_version_comparison(self):
        self.assertEqual(launcher.compare_versions('1.5.0','1.5'),0)
        self.assertGreater(launcher.compare_versions('1.5.0','1.4.9'),0)
        self.assertLess(launcher.compare_versions('1.5.0','2.0.0'),0)

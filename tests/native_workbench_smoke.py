"""Standard-library Linux kernel acceptance, independent of model dependencies.

Run: python3 tests/native_workbench_smoke.py
The runtime package facade imports optional model dependencies. This harness
loads its package path without that facade; the actual guarded_fs and path
contract modules are imported unchanged. No file or restoration code is mocked.
"""

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if "agent_runtime" not in sys.modules:
    runtime = types.ModuleType("agent_runtime")
    runtime.__path__ = [str(ROOT / "agent_runtime")]
    sys.modules["agent_runtime"] = runtime

from src.workbench.files import PRIVATE, ProjectFiles  # noqa: E402
from src.workbench.models import ConflictError  # noqa: E402
from src.workbench.restore import RestoreCoordinator  # noqa: E402
from src.workbench.store import WorkbenchStore  # noqa: E402


@unittest.skipUnless(sys.platform == "linux", "Linux native acceptance")
class NativeRestore(unittest.TestCase):
    def setUp(self):  # noqa: N802
        self.temporary = tempfile.TemporaryDirectory(prefix="fixloop-s3-")
        self.directory = Path(self.temporary.name)
        self.repo = self.directory / "project"
        self.repo.mkdir()
        self.state = self.directory / "state"
        self.state.mkdir()
        self.store = WorkbenchStore(self.state / "workbench.sqlite3")
        self.coordinator = RestoreCoordinator(
            self.store, self.state, lock_dir=self.directory / "locks"
        )
        (self.repo / "a").write_bytes(b"base")
        self.chat = self.coordinator.create("native", self.repo)

    def tearDown(self):  # noqa: N802
        self.coordinator.close()
        self.temporary.cleanup()

    def apply(self, **kwargs):
        preview = self.coordinator.preview(self.chat.id, action="fork", **kwargs)
        self.coordinator.apply(self.chat.id, preview["id"])
        return preview

    def test_binary_and_type_transitions(self):
        (self.repo / "d").mkdir()
        (self.repo / "d" / "data").write_bytes(bytes(range(256)) * 1000)
        saved = self.coordinator.checkpoint(self.chat.id)
        (self.repo / "d" / "data").unlink()
        (self.repo / "d").rmdir()
        (self.repo / "d").write_bytes(b"file")
        (self.repo / "new").write_bytes(b"new")
        self.apply(node_id=saved.id)
        self.assertEqual((self.repo / "d" / "data").read_bytes(), bytes(range(256)) * 1000)
        self.assertFalse((self.repo / "new").exists())
        self.assertFalse(list(self.repo.glob(PRIVATE + "*")))

    def test_modes_and_manual_branch(self):
        (self.repo / "a").chmod(0o444)
        saved = self.coordinator.checkpoint(self.chat.id)
        (self.repo / "a").chmod(0o644)
        (self.repo / "a").write_bytes(b"manual")
        self.apply(node_id=saved.id)
        self.assertEqual((self.repo / "a").stat().st_mode & 0o777, 0o444)
        preview = self.coordinator.preview(
            self.chat.id, action="switch", branch_id=self.chat.active_branch_id
        )
        self.coordinator.apply(self.chat.id, preview["id"])
        self.assertEqual((self.repo / "a").read_bytes(), b"manual")
        self.assertEqual((self.repo / "a").stat().st_mode & 0o777, 0o644)

    def test_stale_plan(self):
        preview = self.coordinator.preview(self.chat.id, action="fork")
        (self.repo / "a").write_bytes(b"edit")
        with self.assertRaises(ConflictError):
            self.coordinator.apply(self.chat.id, preview["id"])
        self.assertEqual((self.repo / "a").read_bytes(), b"edit")

    def test_directory_mode_ignores_process_umask(self):
        directory = self.repo / "directory"
        directory.mkdir()
        directory.chmod(0o755)
        saved = self.coordinator.checkpoint(self.chat.id)
        directory.rmdir()
        previous = os.umask(0o077)
        try:
            self.apply(node_id=saved.id)
        finally:
            os.umask(previous)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)

    def test_restrictive_directory_contents_and_recovery(self):
        directory = self.repo / "directory"
        directory.mkdir()
        leaf = directory / "leaf"
        leaf.write_bytes(b"base")
        directory.chmod(0o555)
        saved = self.coordinator.checkpoint(self.chat.id)
        try:
            for decision in ("complete", "rollback"):
                directory.chmod(0o755)
                leaf.write_bytes(b"manual")
                directory.chmod(0o555)
                preview = self.coordinator.preview(self.chat.id, action="fork", node_id=saved.id)

                def fault(where, index):
                    if where == "after_step" and index == 0:
                        raise RuntimeError("interrupted after permission preparation")

                self.coordinator.fault = fault
                with self.assertRaises(RuntimeError):
                    self.coordinator.apply(self.chat.id, preview["id"])
                self.coordinator.fault = lambda *_: None
                self.coordinator.recover(self.chat.id, preview["id"], decision=decision)
                self.assertEqual(
                    leaf.read_bytes(), b"base" if decision == "complete" else b"manual"
                )
                self.assertEqual(directory.stat().st_mode & 0o777, 0o555)
            directory.chmod(0o755)
            leaf.unlink()
            directory.rmdir()
            self.apply(node_id=saved.id)
            self.assertEqual(leaf.read_bytes(), b"base")
            self.assertEqual(directory.stat().st_mode & 0o777, 0o555)
        finally:
            directory.chmod(0o755)

    def test_links_and_special_files(self):
        outside = self.directory / "outside"
        outside.write_bytes(b"safe")
        alias = self.repo / "alias"
        for create in (
            lambda: alias.symlink_to(outside),
            lambda: os.link(outside, alias),
            lambda: os.mkfifo(alias),
        ):
            create()
            with self.assertRaises((ValueError, OSError)):
                self.coordinator.preview(self.chat.id, action="fork")
            alias.unlink()
        self.assertEqual(outside.read_bytes(), b"safe")

    def test_recovery_and_retry(self):
        for decision in ("complete", "rollback"):
            (self.repo / "a").write_bytes(b"manual")
            preview = self.coordinator.preview(self.chat.id, action="fork")

            def fault(where, index):
                if where == "after_step":
                    raise RuntimeError("interrupted")

            self.coordinator.fault = fault
            with self.assertRaises(RuntimeError):
                self.coordinator.apply(self.chat.id, preview["id"])
            self.coordinator.fault = lambda *_: None
            self.coordinator.recover(self.chat.id, preview["id"], decision=decision)
            self.assertEqual(
                (self.repo / "a").read_bytes(), b"base" if decision == "complete" else b"manual"
            )
            self.assertFalse(self.coordinator.pending(self.chat.id))

    def test_root_replacement_detected(self):
        files = ProjectFiles(self.repo)
        try:
            self.repo.rename(self.directory / "moved")
            self.repo.mkdir()
            with self.assertRaises(ConflictError):
                files.scan()
        finally:
            files.close()

    def test_new_file_during_publication(self):
        (self.repo / "a").write_bytes(b"manual")
        preview = self.coordinator.preview(self.chat.id, action="fork")
        files = self.coordinator.project(self.chat.id)[1]
        original = files._rename

        def racing(parent, source, target, destination=None):
            result = original(parent, source, target, destination)
            (self.repo / "a").write_bytes(b"editor")
            return result

        files._rename = racing
        with self.assertRaises(OSError):
            self.coordinator.apply(self.chat.id, preview["id"])
        self.assertEqual((self.repo / "a").read_bytes(), b"editor")
        with self.assertRaises(ConflictError):
            self.coordinator.recover(self.chat.id, preview["id"], decision="complete")

    def test_process_crash(self):
        self.coordinator.close()
        script = """
import os, sys
from pathlib import Path
from native_workbench_smoke import ROOT, RestoreCoordinator, WorkbenchStore
p=Path(sys.argv[1])
s=WorkbenchStore(p/'state'/'workbench.sqlite3')
def fault(where,index):
    if where=='after_step': os._exit(73)
c=RestoreCoordinator(s,p/'state',lock_dir=p/'locks',fault=fault)
chat=s.list_conversations()[0]
(p/'project'/'a').write_bytes(b'manual')
plan=c.preview(chat.id,action='fork')
c.apply(chat.id,plan['id'])
"""
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "tests")}
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.directory)],
            env=environment,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(child.returncode, 73, child.stderr.decode())
        self.coordinator = RestoreCoordinator(
            self.store, self.state, lock_dir=self.directory / "locks"
        )
        operation = self.coordinator.pending(self.chat.id)[0]
        self.coordinator.recover(self.chat.id, operation["id"], decision="rollback")
        self.assertEqual((self.repo / "a").read_bytes(), b"manual")


if __name__ == "__main__":
    unittest.main()

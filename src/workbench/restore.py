"""Preview-bound restoration, project ownership and explicit crash recovery."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from src.workbench.files import ProjectFiles, digest
from src.workbench.models import ConflictError, NotFoundError
from src.workbench.ownership import ServiceOwnership
from src.workbench.snapshots import SnapshotRepository, canonical, sync_directory


def overlaps(a, b):
    return a == b or a in b.parents or b in a.parents


class ProjectOwnership:
    """Registry lock makes nested-root checks atomic across state directories."""

    def __init__(self, directory, root):
        directory.mkdir(parents=True, exist_ok=True)
        key = digest(os.path.normcase(str(root)).encode())
        self.lock = None
        with_owner = ServiceOwnership(directory / "registry.lock")
        try:
            registry = directory / "projects.json"
            records = json.loads(registry.read_text()) if registry.exists() else {}
            active = {}
            for old_key, old_root in records.items():
                try:
                    probe = ServiceOwnership(directory / (old_key + ".lock"))
                except ConflictError:
                    active[old_key] = old_root
                    if overlaps(root, Path(old_root)):
                        raise ConflictError(
                            "another service owns this project or an overlapping root"
                        )
                else:
                    probe.close()
            self.lock = ServiceOwnership(directory / (key + ".lock"))
            active[key] = str(root)
            temp = directory / (".registry-" + uuid.uuid4().hex)
            try:
                with temp.open("xb") as stream:
                    stream.write(canonical(active))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, registry)
                sync_directory(directory)
            finally:
                temp.unlink(missing_ok=True)
        except BaseException:
            self.close()
            raise
        finally:
            with_owner.close()

    def close(self):
        if self.lock:
            self.lock.close()
            self.lock = None


class RestoreJournal:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations "
                "(id TEXT PRIMARY KEY, root TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def write(self, plan):
        with self.connect() as db:
            db.execute(
                "INSERT INTO operations VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (plan["id"], plan["root"], canonical(plan).decode()),
            )

    def read(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM operations WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise NotFoundError("restore operation")
        return json.loads(row[0])

    def all(self):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT payload FROM operations ORDER BY rowid")
            ]


def steps(before, target):
    """Remove children before parents; create parents before children."""
    prepare, finish = [], []
    if os.name != "nt":
        # Temporarily permit child writes, then restore directory permissions
        # after their contents. These intermediate modes are journaled too.
        before = dict(before)
        parents = set()
        for path in set(before) | set(target):
            if before.get(path) != target.get(path):
                parts = path.split("/")
                parents.update("/".join(parts[:i]) for i in range(1, len(parts)))
        for path in sorted(parents, key=lambda p: (p.count("/"), p)):
            old = before.get(path)
            if old and old["kind"] == "directory" and old["mode"] & 0o700 != 0o700:
                writable = {**old, "mode": old["mode"] | 0o700}
                prepare.append({"path": path, "before": old, "after": writable})
                before[path] = writable
    remove, directories, files = [], [], []
    for path in sorted(set(before) | set(target)):
        old, new = before.get(path), target.get(path)
        if old == new:
            continue
        if old and (new is None or old["kind"] != new["kind"]):
            remove.append({"path": path, "before": old, "after": None})
            old = None
        if new:
            if os.name != "nt" and new["kind"] == "directory":
                writable = {**new, "mode": new["mode"] | 0o700} if old is None else old
                if old is None:
                    directories.append({"path": path, "before": None, "after": writable})
                if writable != new:
                    finish.append({"path": path, "before": writable, "after": new})
                continue
            item = {"path": path, "before": old, "after": new}
            (directories if new["kind"] == "directory" else files).append(item)
    remove.sort(key=lambda s: (-s["path"].count("/"), s["path"]))
    directories.sort(key=lambda s: (s["path"].count("/"), s["path"]))
    finish.sort(key=lambda s: (-s["path"].count("/"), s["path"]))
    return prepare + remove + directories + files + finish


class RestoreCoordinator:
    def __init__(self, store, state_dir, *, lock_dir=None, fault=None):
        self.store = store
        self.state_dir = state_dir
        self.repository = SnapshotRepository(state_dir / "snapshots")
        self.journal = RestoreJournal(state_dir / "restores.sqlite3")
        self.lock_dir = Path(lock_dir) if lock_dir else Path.home() / ".fixloop" / "project-locks"
        self.fault = fault or (lambda *_: None)
        self.projects = {}
        self.plans = {}
        try:
            for conversation in self.store.list_conversations():
                if (
                    conversation.project_path is not None
                    and conversation.base_snapshot_id is not None
                ):
                    self._files(conversation.project_path)
        except BaseException:
            self.close()
            raise

    def close(self):
        for files, owner in self.projects.values():
            files.close()
            owner.close()
        self.projects.clear()

    def _files(self, root):
        root = Path(root).expanduser().absolute()
        if root in self.projects:
            return self.projects[root][0]
        files = ProjectFiles(root)
        root = files.root
        try:
            if overlaps(root, self.state_dir) or overlaps(root, self.lock_dir.resolve()):
                raise ValueError("project and workbench storage must be disjoint")
            if any(overlaps(root, existing) for existing in self.projects):
                raise ConflictError("overlapping project roots")
            owner = ProjectOwnership(self.lock_dir, root)
        except BaseException:
            files.close()
            raise
        self.projects[root] = (files, owner)
        return files

    def project(self, cid, *, recovery=False):
        conversation = self.store.get_conversation(cid)
        if conversation.project_path is None or conversation.base_snapshot_id is None:
            raise ValueError("operation requires a project baseline")
        files = self._files(conversation.project_path)
        if not recovery and self.pending(cid):
            raise ConflictError("project has an unfinished restore; choose complete or rollback")
        return conversation, files

    def create(self, title, project_path):
        files = self._files(project_path)
        if any(c.project_path == str(files.root) for c in self.store.list_conversations()):
            raise ConflictError("S3 supports one conversation per project; reuse its conversation")
        if any(
            p["root"] == str(files.root) and p["status"] not in {"committed", "rolled_back"}
            for p in self.journal.all()
        ):
            raise ConflictError("project has an unfinished restore")
        try:
            key, manifest = self.repository.save(files)
            return self.store.create_conversation(
                title, project_path=str(files.root), baseline=(key, manifest)
            )
        except BaseException:
            files, owner = self.projects.pop(files.root)
            files.close()
            owner.close()
            raise

    def _snapshot(self, tree, sid, files):
        snapshot = next((s for s in tree.snapshots if s.id == sid), None)
        if snapshot is None:
            raise ValueError("history has no file snapshot")
        manifest = self.repository.load(snapshot.storage_key, snapshot.manifest)
        if manifest["root_identity"] != files.identity:
            raise ConflictError("snapshot belongs to a different project root identity")
        return snapshot.storage_key, manifest

    def _receipt(self, plan):
        tree = self.store.get_tree(plan["conversation_id"])
        return any(s.manifest.get("restore_operation") == plan["id"] for s in tree.snapshots)

    def pending(self, cid):
        # Read only: even a receipt-confirmed operation needs explicit recovery
        # for backup cleanup. Startup never mutates project files.
        return [
            self.public(p)
            for p in self.journal.all()
            if p["conversation_id"] == cid
            and "parent" not in p
            and p["status"] not in {"committed", "rolled_back"}
        ]

    @staticmethod
    def public(plan):
        return {
            k: plan[k]
            for k in ("id", "conversation_id", "action", "revision", "manual", "changes", "status")
        }

    def status(self, cid):
        conversation, files = self.project(cid, recovery=True)
        tree = self.store.get_tree(cid)
        branch = next(b for b in tree.branches if b.id == conversation.active_branch_id)
        _, expected = self._snapshot(
            tree, self.store.get_effective_snapshot(cid, branch.head_node_id), files
        )
        live = files.scan()
        return {
            "manual_changes": live != expected,
            "pending": self.pending(cid),
            "policy": live["policy"],
        }

    def checkpoint(self, cid, *, label="本地修改", expected_revision=None):
        conversation, files = self.project(cid)
        if expected_revision is not None and expected_revision != conversation.revision:
            raise ConflictError("stale conversation revision")
        self._idle(cid)
        key, manifest = self.repository.save(files)
        return self.store.save_checkpoint(
            cid, key, manifest, label=label, expected_revision=conversation.revision
        )

    def _idle(self, cid):
        if any(r.status in {"queued", "running"} for r in self.store.get_tree(cid).runs):
            raise ConflictError("finish or cancel the active turn before restoring files")

    def preview(
        self, cid, *, action, node_id=None, branch_id=None, name="恢复分支", expected_revision=None
    ):
        conversation, files = self.project(cid)
        self._idle(cid)
        if expected_revision is not None and conversation.revision != expected_revision:
            raise ConflictError("stale conversation revision")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("branch name must be nonempty")
        tree = self.store.get_tree(cid)
        source = next(b for b in tree.branches if b.id == conversation.active_branch_id)
        _, source_manifest = self._snapshot(
            tree, self.store.get_effective_snapshot(cid, source.head_node_id), files
        )
        destination = node_id
        if action == "switch":
            if node_id is not None:
                raise ValueError("switch requires only a branch_id")
            branch = next((b for b in tree.branches if b.id == branch_id), None)
            if branch is None:
                raise NotFoundError("branch")
            destination = branch.head_node_id
            sid = self.store.get_effective_snapshot(cid, destination)
        elif action == "fork":
            if branch_id is not None:
                raise ValueError("fork requires only a node_id")
            sid = self.store.get_effective_snapshot(cid, node_id)
        elif action == "input":
            if branch_id is not None:
                raise ValueError("input requires only a node_id")
            node = next((n for n in tree.nodes if n.id == node_id and n.kind == "turn"), None)
            if node is None:
                raise NotFoundError("turn node")
            sid, destination = node.input_snapshot_id, node.parent_id
        else:
            raise ValueError("action must be switch, fork or input")
        target_key, target = self._snapshot(tree, sid, files)
        before_key, before = self.repository.save(files)
        files.preflight(before["entries"], target["entries"])
        changes = [
            {
                "path": path,
                "before": before["entries"].get(path),
                "after": target["entries"].get(path),
            }
            for path in sorted(set(before["entries"]) | set(target["entries"]))
            if before["entries"].get(path) != target["entries"].get(path)
        ]
        plan = {
            "id": "restore-" + uuid.uuid4().hex,
            "conversation_id": cid,
            "root": str(files.root),
            "revision": conversation.revision,
            "action": action,
            "branch_id": branch_id,
            "destination_node": destination,
            "name": name,
            "manual": before != source_manifest,
            "before_key": before_key,
            "before": before,
            "target_key": target_key,
            "target": target,
            "changes": changes,
            "status": "preview",
            "created_at": time.time(),
            "steps": steps(before["entries"], target["entries"]),
            "started": -1,
            "done": -1,
        }
        self.plans = {k: v for k, v in self.plans.items() if time.time() - v["created_at"] < 600}
        if len(self.plans) >= 32:
            raise ConflictError("restore preview capacity reached")
        self.plans[plan["id"]] = plan
        return self.public(plan)

    def apply(self, cid, plan_id):
        conversation, files = self.project(cid)
        plan = self.plans.get(plan_id)
        if plan is None or plan["conversation_id"] != cid:
            raise NotFoundError("restore preview")
        if time.time() - plan["created_at"] >= 600:
            raise ConflictError("restore preview expired")
        if conversation.revision != plan["revision"]:
            raise ConflictError("restore preview revision is stale")
        self._idle(cid)
        self._validate(plan, files)
        if files.scan() != plan["before"]:
            raise ConflictError("workspace changed since restore preview")
        files.preflight(plan["before"]["entries"], plan["target"]["entries"])
        plan["status"] = "applying"
        self.journal.write(plan)
        self.plans.pop(plan_id)
        return self._execute(plan, files)

    def _validate(self, plan, files):
        for prefix in ("before", "target"):
            self.repository.load(plan[prefix + "_key"], plan[prefix])
            if plan[prefix]["root_identity"] != files.identity:
                raise ConflictError("project root identity changed")

    def _execute(self, plan, files):
        try:
            for index, step in enumerate(plan["steps"]):
                if index <= plan["done"]:
                    continue
                plan["started"] = index
                self.journal.write(plan)
                self.fault("before_step", index)
                target = step["after"]
                data = (
                    self.repository.blob(target["digest"])
                    if target and target["kind"] == "file"
                    else None
                )
                files.change(
                    step["path"], step["before"], target, data, plan["id"][8:] + f"-{index}"
                )
                self.fault("after_step", index)
                plan["done"] = index
                self.journal.write(plan)
            if files.scan() != plan["target"]:
                raise ConflictError("workspace changed during restore")
            plan["status"] = "files_applied"
            self.journal.write(plan)
            self.fault("before_commit", -1)
            self.store.commit_restore(plan)
            self.fault("after_commit", -1)
            self._cleanup(plan, files)
            plan["status"] = "committed"
            self.journal.write(plan)
            return self.public(plan)
        except BaseException:
            plan["status"] = "needs_recovery"
            self.journal.write(plan)
            raise

    def _cleanup(self, plan, files):
        for index, step in enumerate(plan["steps"]):
            files.cleanup(plan["id"][8:] + f"-{index}", step["path"], step["before"])

    def _known_live(self, plan, files):
        live = files.scan()
        before, target = plan["before"]["entries"], plan["target"]["entries"]
        touched = {}
        for step in plan["steps"][: plan["started"] + 1]:
            touched.setdefault(step["path"], []).extend([step["before"], step["after"]])
        for path in set(live["entries"]) | set(before) | set(target):
            actual = live["entries"].get(path)
            allowed = [before.get(path)]
            if path in touched:
                allowed += touched[path] + [None]
            if actual not in allowed:
                raise ConflictError("new workspace edits conflict with recovery; preserved")
        return live

    def recover(self, cid, operation_id, *, decision):
        _, files = self.project(cid, recovery=True)
        plan = self.journal.read(operation_id)
        if plan["conversation_id"] != cid:
            raise NotFoundError("restore operation")
        if "parent" in plan:
            raise ValueError("recover using the original operation ID")
        if decision not in {"complete", "rollback"}:
            raise ValueError("decision must be complete or rollback")
        if plan["status"] in {"committed", "rolled_back"}:
            return self.public(plan)
        self._validate(plan, files)
        if self._receipt(plan):
            if decision != "complete":
                raise ConflictError(
                    "history already committed; complete recovery, then preview another restore"
                )
            if files.scan() != plan["target"]:
                raise ConflictError("new workspace edits conflict with committed recovery")
            self._cleanup(plan, files)
            for child in self.journal.all():
                if child.get("parent") == operation_id:
                    self._cleanup(child, files)
                    child["status"] = "committed"
                    self.journal.write(child)
            plan["status"] = "committed"
            self.journal.write(plan)
            return self.public(plan)
        if self.store.get_conversation(cid).revision != plan["revision"]:
            raise ConflictError("history changed during interrupted restore")
        # Persist a recovery child before any further writes. Interrupted
        # recoveries use this child's step journal for exact preimage checks.
        children = [
            p
            for p in self.journal.all()
            if p.get("parent") == operation_id and p["status"] != "rolled_back"
        ]
        if children:
            child = children[-1]
            if child["decision"] != decision:
                raise ConflictError("continue the already chosen recovery decision")
            return self._recover_child(plan, child, files)
        live = self._known_live(plan, files)
        destination = plan["target"] if decision == "complete" else plan["before"]
        files.preflight(live["entries"], destination["entries"])
        before_key, coherent = self.repository.save(files)
        if coherent != live:
            raise ConflictError("workspace changed while preparing recovery")
        child = {
            **plan,
            "id": "restore-" + uuid.uuid4().hex,
            "parent": operation_id,
            "decision": decision,
            "before": live,
            "before_key": before_key,
            "target": destination,
            "target_key": plan["target_key"] if decision == "complete" else plan["before_key"],
            "steps": steps(live["entries"], destination["entries"]),
            "started": -1,
            "done": -1,
            "status": "recovering",
        }
        self.journal.write(child)
        return self._recover_child(plan, child, files)

    def _recover_child(self, original, child, files):
        try:
            self._validate(child, files)
            self._known_live(child, files)
            for index, step in enumerate(child["steps"]):
                # A path can have a remove + create pair. Use fresh reads as
                # earlier steps change it; a completed interrupted step is safe.
                actual, _ = files.read(step["path"])
                if index <= child["done"]:
                    continue
                if index == child["started"] and actual == step["after"]:
                    child["done"] = index
                    self.journal.write(child)
                    continue
                allowed = [step["before"]]
                if index == child["started"]:
                    allowed.append(None)  # source moved, target not yet published
                if actual not in allowed:
                    raise ConflictError("workspace changed after recovery validation")
                child["started"] = index
                self.journal.write(child)
                self.fault("recovery_before_step", index)
                target = step["after"]
                data = (
                    self.repository.blob(target["digest"])
                    if target and target["kind"] == "file"
                    else None
                )
                files.change(step["path"], actual, target, data, child["id"][8:] + f"-{index}")
                child["done"] = index
                self.journal.write(child)
            if files.scan() != child["target"]:
                raise ConflictError("workspace changed during recovery")
            if child["decision"] == "complete" and not self._receipt(original):
                self.store.commit_restore(original)
            self._cleanup(child, files)
            self._cleanup(original, files)
            original["status"] = "committed" if child["decision"] == "complete" else "rolled_back"
            # Resolve both records durably; original receipt reconciles complete.
            child["status"] = original["status"]
            self.journal.write(child)
            self.journal.write(original)
            return self.public(original)
        except BaseException:
            child["status"] = "needs_recovery"
            self.journal.write(child)
            raise

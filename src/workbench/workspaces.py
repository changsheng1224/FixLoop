"""Materialize immutable inputs without writable links to a registered project."""

from contextlib import closing

from src.workbench.files import ProjectFiles
from src.workbench.restore import steps


class ExecutionWorkspaces:
    def __init__(self, state_dir, repository):
        self.root = state_dir / "workspaces"
        self.repository = repository
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, run_id):
        if not run_id.startswith("run-") or len(run_id) != 36:
            raise ValueError("invalid execution workspace ID")
        int(run_id[4:], 16)
        return self.root / run_id / "tree"

    def create(self, run_id, manifest, token):
        root = self.path(run_id)
        root.mkdir(parents=True, exist_ok=False)
        with closing(ProjectFiles(root)) as files:
            for index, step in enumerate(steps({}, manifest["entries"])):
                token.check()
                entry = step["after"]
                content = self.repository.blob(entry["digest"]) if entry["kind"] == "file" else None
                files.change(step["path"], step["before"], entry, content, f"materialize-{index}")
            if files.scan()["entries"] != manifest["entries"]:
                raise ValueError("execution input materialization mismatch")
        return root

    def capture(self, run_id, root_identity):
        with closing(ProjectFiles(self.path(run_id))) as files:
            return self.repository.save(files, root_identity=root_identity)

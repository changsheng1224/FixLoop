"""User-facing repair lifecycle: input, workspace, runtime and delivery."""

from __future__ import annotations

import difflib
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

from agent_runtime.bootstrap import load_dotenv
from agent_runtime.cancellation import CancellationToken, CancelledError
from agent_runtime.signal_cancel import sigint_cancel_scope
from src.cli_exit_codes import REPAIR_EXIT_CONFIG, REPAIR_EXIT_FAIL, repair_exit_code
from src.repair.language_detect import detect_repair_language
from src.repair_report import RepairReport
from src.repair_workspace import (
    GitSnapshot,
    RepairInputError,
    clone_repository,
    deliverable_path,
    git_run,
    parse_repo_source,
    validate_ref,
)
from src.state import RepairState


class TextSnapshot:
    """Non-Git demo directories retain their existing in-place repair contract."""

    base_commit = ""

    def __init__(self, repo: Path):
        self.repo = repo
        self.before = self._read()

    def _read(self) -> dict[str, bytes]:
        result = {}
        for directory, dirs, files in os.walk(self.repo):
            relative = Path(directory).relative_to(self.repo)
            dirs[:] = [name for name in dirs if deliverable_path(str(relative / name))]
            for name in files:
                path = Path(directory) / name
                rel = path.relative_to(self.repo).as_posix()
                if deliverable_path(rel) and not path.is_symlink():
                    result[rel] = path.read_bytes()
        return result

    def export(self) -> tuple[bytes, list[str]]:
        after = self._read()
        changed = sorted(
            name
            for name in self.before.keys() | after.keys()
            if self.before.get(name) != after.get(name)
        )
        patches = []
        for name in changed:
            old, new = self.before.get(name, b""), after.get(name, b"")
            if b"\0" in old or b"\0" in new:
                raise RepairInputError("export_failed", "非 Git 目录的二进制修改不支持导出")
            old_label = f"a/{name}" if name in self.before else "/dev/null"
            new_label = f"b/{name}" if name in after else "/dev/null"
            diff = list(
                difflib.unified_diff(
                    old.decode("utf-8").splitlines(keepends=True),
                    new.decode("utf-8").splitlines(keepends=True),
                    fromfile=old_label,
                    tofile=new_label,
                )
            )
            patches.extend(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                for line in diff
            )
        return "".join(patches).encode("utf-8"), changed

    def close(self) -> None:
        pass


def read_issue(args) -> str:
    try:
        issue = (
            Path(args.issue_file).read_text(encoding="utf-8-sig")
            if getattr(args, "issue_file", None)
            else args.issue
        )
    except (OSError, UnicodeError) as exc:
        raise RepairInputError("input_invalid", f"无法读取 --issue-file: {exc}") from exc
    if not issue or not issue.strip():
        raise RepairInputError("input_invalid", "问题描述不能为空")
    return issue.strip()


def run_repair_cli(
    args,
    execute: Callable[[str, CancellationToken], RepairState],
    on_result: Callable[[RepairState], None] | None = None,
) -> int:
    report = None
    snapshot = None
    token = CancellationToken()
    state = None
    exit_code = REPAIR_EXIT_FAIL
    try:
        if getattr(args, "execution_backend", "legacy") == "wsl_bwrap":
            if args.execution_tier != "auto" or args.require_sandbox:
                raise RepairInputError(
                    "configuration_failed",
                    "wsl_bwrap conflicts with legacy tier/require-sandbox",
                )
            if args.code_exploration_mode != "text" or args.pylsp_path:
                raise RepairInputError("configuration_failed", "wsl_bwrap does not support LSP")
            raise RepairInputError(
                "configuration_failed",
                "wsl_bwrap repair requires P3 external state_root isolation",
            )
        load_dotenv()
        # Validate addresses before persisting them, so credentials never enter reports.
        source = parse_repo_source(args.repo)
        args.issue = read_issue(args)
        ref = getattr(args, "ref", None)
        validate_ref(ref)
        if not source.remote and ref:
            raise RepairInputError("input_invalid", "--ref 仅用于 GitHub 仓库输入")
        if source.remote and getattr(args, "resume_repair", None):
            raise RepairInputError("input_invalid", "续跑请使用已生成的本地工作目录 --repo")
        output_arg = getattr(args, "output", None)
        output = (
            Path(output_arg).expanduser().resolve()
            if output_arg
            else Path.cwd() / ".fixloop" / "runs" / uuid.uuid4().hex
        )
        if not source.remote and output.is_relative_to(Path(source.location)):
            # Artifact paths must not enter repair context or text snapshots.
            if ".fixloop" not in output.relative_to(Path(source.location)).parts:
                raise RepairInputError("input_invalid", "本地 --output 请放在仓库外或 .fixloop 内")
        if output.exists():
            raise RepairInputError("input_invalid", f"结果目录已存在: {output}")
        output.mkdir(parents=True, exist_ok=False)
        report = RepairReport(output, source.location)
        report.data["requested_ref"] = ref or ""
        report.data["dry_run"] = bool(getattr(args, "dry_run", False))

        def progress(phase: str, message: str) -> None:
            report.progress(phase, message)
            print(f"[FixLoop:{phase}] {message}", file=sys.stderr)

        with sigint_cancel_scope(token, first_message="[FixLoop] 取消中…"):
            progress("preflight", "检查 API 配置与输入")
            if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
                raise RepairInputError("configuration_failed", "未设置 DEEPSEEK_API_KEY")
            repo = output / "repo" if source.remote else Path(source.location)
            report.data["repo_path"] = str(repo)
            report.save()
            if source.remote:
                progress("clone", "拉取仓库并固定基线版本")
                report.data["base_commit"] = clone_repository(source, repo, ref=ref, token=token)
                report.save()
                language, _ = detect_repair_language(args.issue, repo_root=repo)
                files = git_run(repo, "ls-files", "-z", token=token).stdout.decode().split("\0")
                if language != "python" or not any(
                    name.endswith((".py", ".pyw")) for name in files
                ):
                    raise RepairInputError(
                        "unsupported_project", "GitHub 入口首期支持 Python/pytest 仓库"
                    )
                if args.execution_tier == "auto" and not args.skip_verify:
                    args.execution_tier = "container"
                if args.execution_tier == "container" and not args.skip_verify:
                    args.require_sandbox = True
            progress("snapshot", "记录修复前工作树；补丁导出使用独立 Git index")
            top = git_run(repo, "rev-parse", "--show-toplevel", check=False, token=token)
            is_git_root = (
                top.returncode == 0
                and Path(top.stdout.decode().strip()).resolve() == repo.resolve()
            )
            snapshot = GitSnapshot(repo, output, token=token) if is_git_root else TextSnapshot(repo)
            report.data["base_commit"] = snapshot.base_commit
            report.data["baseline_tree"] = getattr(snapshot, "baseline", "")
            report.data["verification_tier"] = (
                "skipped" if args.skip_verify else args.execution_tier
            )
            report.save()
            if token.is_cancelled:
                raise CancelledError("user")
            progress("repair", "初始化运行时与验证环境，执行修复")
            state = execute(str(repo), token)
            tier = "skipped" if args.skip_verify else args.execution_tier
            report.record_state(state, tier)
            exit_code = repair_exit_code(state)
            if str(state.status) == "user_cancel":
                exit_code = 130
            progress("delivery", "导出最终工作树补丁与验证报告")
    except (CancelledError, KeyboardInterrupt):
        exit_code = 130
        if report:
            report.fail("user_cancel", "用户取消；工作目录和已有补丁保留")
    except RepairInputError as exc:
        exit_code = 3 if exc.category == "timeout" else REPAIR_EXIT_CONFIG
        print(f"错误: {exc}", file=sys.stderr)
        if report:
            report.fail(exc.category, str(exc))
    except Exception as exc:
        from src.repair_factory import RequiredVerifierError

        category = (
            "verification_environment_failed"
            if isinstance(exc, RequiredVerifierError)
            else "runtime_failed"
        )
        exit_code = (
            REPAIR_EXIT_CONFIG if isinstance(exc, RequiredVerifierError) else REPAIR_EXIT_FAIL
        )
        print(f"错误: {exc}", file=sys.stderr)
        if report:
            report.fail(category, str(exc))
    finally:
        if report and snapshot:
            try:
                patch, changed = snapshot.export()
                (report.output / "patch.diff").write_bytes(patch)
                report.data.update(patch_available=bool(patch), changed_files=changed)
                if exit_code == 0 and not patch and not getattr(args, "dry_run", False):
                    report.fail("no_changes", "运行时未留下可导出的修改，请检查修复日志")
                    exit_code = REPAIR_EXIT_FAIL
                report.save()
            except KeyboardInterrupt:
                exit_code = 130
                report.fail("user_cancel", "用户取消补丁导出；工作目录保留")
            except Exception as exc:
                report.data["export_error"] = str(exc)
                report.data["delivery_status"] = "export_failed"
                report.save()
                print(f"错误: 补丁导出失败: {exc}", file=sys.stderr)
                if exit_code == 0:
                    exit_code = REPAIR_EXIT_FAIL
            finally:
                try:
                    snapshot.close()
                except OSError as exc:
                    print(f"错误: 无法清理临时 index: {exc}", file=sys.stderr)
        if report:
            if (
                state
                and on_result
                and not report.data.get("export_error")
                and report.data["category"] != "no_changes"
            ):
                on_result(state)
            print(f"[FixLoop] 状态={report.data['status']}；结果目录: {report.output}")
    return exit_code

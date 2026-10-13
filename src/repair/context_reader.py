"""Bounded repository context reads with no orchestration dependencies."""

from pathlib import Path

from src.state import RepairPlan, RetrievedContext, SuspectLocation


class RepairContextReader:
    def __init__(self, repo_root):
        self._repo_root = repo_root

    def read_code_snippet(self, file_path: str, start_line: int, end_line: int) -> str:
        """预读嫌疑文件上下文。"""
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(self._repo_root) / path
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").split("\n")
        ctx_start = max(0, start_line - 3)
        ctx_end = min(len(lines), end_line + 3)
        block = ["    ```python"]
        for i in range(ctx_start, ctx_end):
            marker = ">>>" if start_line - 1 <= i < end_line else "   "
            block.append(f"    {marker} {lines[i]}")
        block.append("    ```")
        return "\n".join(block)

    def read_line_range(self, file_path: str, start_line: int, end_line: int) -> str:
        """读取文件的原始行（无 markdown 包装），供 diff-only 上下文使用。"""
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(self._repo_root) / path
        if not path.is_file():
            return ""
        lines = path.read_text(encoding="utf-8").split("\n")
        ctx_start = max(0, start_line - 1)
        ctx_end = min(len(lines), end_line)
        return "\n".join(lines[ctx_start:ctx_end])

    def read_test_context(
        self,
        context: RetrievedContext | None,
        suspects: list[SuspectLocation],
        plan: RepairPlan | None = None,
    ) -> list[str]:
        """预读相关测试的定向片段，避免把整个测试文件塞进 Patcher prompt。"""
        max_total_chars = 12_000
        max_file_chars = 5_000
        max_excerpt_lines = 90
        test_paths: list[Path] = []
        target_names: dict[Path, set[str]] = {}

        if context and context.related_tests:
            for item in context.related_tests:
                ref = str(item)
                path = self.resolve_test_path(ref)
                if path and path not in test_paths:
                    test_paths.append(path)
                if path and "::" in ref:
                    target_names.setdefault(path, set()).update(
                        part for part in ref.split("::")[1:] if part
                    )

        for s in suspects:
            guessed = self.guess_test_file(s.file_path, s.function_name or "")
            if guessed and guessed not in test_paths:
                test_paths.append(guessed)
            if guessed and s.function_name:
                target_names.setdefault(guessed, set()).add(s.function_name)

        if not test_paths:
            test_paths = self.discover_repo_test_files()

        blocks: list[str] = []
        used_chars = 0
        for path in test_paths:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            excerpt = self.select_test_excerpt(
                content,
                target_names.get(path, set()),
                max_chars=max_file_chars,
                max_lines=max_excerpt_lines,
            )
            if not excerpt:
                continue
            remaining = max_total_chars - used_chars
            if remaining <= 0:
                break
            if len(excerpt) > remaining:
                excerpt = excerpt[:remaining].rstrip() + "\n# ... excerpt truncated ..."
            rel = path.name
            blocks.append(f"  {rel}:")
            blocks.append("  ```python")
            blocks.append(excerpt)
            blocks.append("  ```")
            used_chars += len(excerpt)
        return blocks

    @staticmethod
    def select_test_excerpt(
        content: str,
        target_names: set[str],
        *,
        max_chars: int,
        max_lines: int,
    ) -> str:
        """Keep the relevant test definition, with a bounded head/tail fallback."""
        lines = content.splitlines()
        if not lines:
            return ""
        starts: list[int] = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not target_names:
                continue
            for name in target_names:
                if stripped.startswith((f"def {name}", f"class {name}")):
                    starts.append(index)
                    break
        if starts:
            center = starts[0]
            begin = max(0, center - 8)
            end = min(len(lines), center + max_lines - 8)
            selected = lines[begin:end]
        elif len(lines) <= max_lines:
            selected = lines
        else:
            head = max_lines // 2
            selected = (
                lines[:head]
                + ["# ... middle of test file omitted ..."]
                + lines[-(max_lines - head) :]
            )
        excerpt = "\n".join(selected).rstrip()
        if len(excerpt) > max_chars:
            excerpt = excerpt[:max_chars].rstrip() + "\n# ... excerpt truncated ..."
        return excerpt

    def resolve_test_path(self, ref: str) -> Path | None:
        """从 pytest nodeid 或路径解析测试文件。"""
        file_part = ref.split("::", 1)[0].strip()
        if not file_part:
            return None
        candidates = [
            Path(self._repo_root) / file_part,
            Path(self._repo_root) / "tests" / file_part,
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None

    def guess_test_file(self, source_file: str, function_name: str) -> Path | None:
        """按约定猜测 test_<module>.py。"""
        stem = Path(source_file).stem
        names = [f"test_{stem}.py", f"{stem}_test.py", "test_app.py"]
        for name in names:
            for base in (Path(self._repo_root), Path(self._repo_root) / "tests"):
                path = base / name
                if not path.is_file():
                    continue
                if not function_name:
                    return path
                text = path.read_text(encoding="utf-8")
                if function_name in text:
                    return path
        return None

    def discover_repo_test_files(self) -> list[Path]:
        """扫描 repo 根目录下的 test_*.py。"""
        root = Path(self._repo_root)
        found: list[Path] = []
        for pattern in ("test_*.py",):
            found.extend(root.glob(pattern))
            tests_dir = root / "tests"
            if tests_dir.is_dir():
                found.extend(tests_dir.glob(pattern))
        return sorted({p.resolve() for p in found})

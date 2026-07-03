"""生成 eval case 的 expected_patch.diff、issue.txt，并校验补丁可修绿。"""

from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES_DIR = ROOT / "src" / "eval" / "cases"

# case_id -> {relative_path: fixed_content}
FIXES: dict[str, dict[str, str]] = {
    "case_001": {
        "pricing.py": '''"""价格计算（含故意的类型转换 bug）。"""


def line_total(unit_price, count):
    """计算行总价。unit_price 可能来自 JSON 字符串。"""
    return int(unit_price) * count
''',
    },
    "case_002": {
        "labels.py": '''"""用户标签格式化（含故意的返回值类型 bug）。"""


def user_label(user_id):
    """返回可用于拼接的用户标签片段。"""
    return str(user_id)


def greet(user_id):
    return "User:" + user_label(user_id)
''',
    },
    "case_003": {
        "concat.py": '''"""安全拼接（含故意的 None 未判断 bug）。"""


def safe_concat(a, b):
    """拼接两段文本；None 应视为空字符串。"""
    a = a if a is not None else ""
    b = b if b is not None else ""
    return a + b
''',
    },
    "case_004": {
        "app.py": '''"""应用入口（含故意的 import 路径 bug）。"""

from utils.helpers import greet


def run() -> str:
    return greet()
''',
    },
    "case_005": {
        "service.py": '''"""服务层（含错误的符号 import）。"""

from utils.helpers import greet


def message() -> str:
    return greet()
''',
    },
    "case_006": {
        "ranges.py": '''"""整数区间（含故意的 off-by-one bug）。"""


def inclusive_range(start: int, end: int) -> list[int]:
    """返回闭区间 [start, end] 的整数列表。"""
    return list(range(start, end + 1))
''',
    },
    "case_007": {
        "users.py": '''"""用户模型（含故意的 None 属性访问 bug）。"""

from dataclasses import dataclass


@dataclass
class Profile:
    display_name: str


@dataclass
class User:
    name: str
    profile: Profile | None = None


def display_name(user: User) -> str:
    if user.profile is None:
        return user.name
    return user.profile.display_name
''',
    },
    "case_008": {
        "transform.py": '''"""分数归一化（hop 2）。"""

from values import clamp


def normalize_score(score: int) -> float:
    """输入已是 0–100 的百分制分数，返回同尺度浮点值。"""
    return float(clamp(score, 0, 100))
''',
    },
    "case_009": {
        "pyproject.toml": '''[project]
name = "eval-case-009"
version = "0.1.0"
requires-python = ">=3.11"

[build-system]
requires = ["setuptools>=75.0"]
build-backend = "setuptools.build_meta"

[tool.pytest.ini_options]
testpaths = ["."]

[tool.eval]
multiplier = 2
''',
    },
    "case_010": {
        "gateway.py": '''"""网关层（含错误的 backend import）。"""

from backend.tasks import run_task


def invoke(a, b):
    return run_task(a, b)
''',
        "backend/tasks.py": '''"""任务执行（含类型运算 bug）。"""


def run_task(a, b):
    return int(a) + int(b)
''',
    },
}

ISSUES: dict[str, str] = {
    "case_001": """TypeError: unsupported operand type(s) for +: 'str' and 'int'

CI log excerpt:
  File "pricing.py", line 6, in line_total
    return unit_price + count
TypeError: unsupported operand type(s) for +: 'str' and 'int'

Failed: test_pricing.py::test_line_total_str_price
Repo: eval-case-001
""",
    "case_002": """TypeError: can only concatenate str (not "int") to str

  File "labels.py", line 10, in greet
    return "User:" + user_label(user_id)
TypeError: can only concatenate str (not "int") to str

Failed: test_labels.py::test_greet_int_id
""",
    "case_003": """TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'

  File "concat.py", line 6, in safe_concat
    return a + b
TypeError: unsupported operand type(s) for +: 'NoneType' and 'str'

Failed: test_concat.py::test_safe_concat_none_left
""",
    "case_004": """ModuleNotFoundError: No module named 'utils.helper'

  File "app.py", line 3, in <module>
    from utils.helper import greet
ModuleNotFoundError: No module named 'utils.helper'

Note: implementation lives in utils/helpers.py
""",
    "case_005": """ImportError: cannot import name 'hello' from 'utils.helpers'

  File "service.py", line 3, in <module>
    from utils.helpers import hello
ImportError: cannot import name 'hello' from 'utils.helpers'
""",
    "case_006": """AssertionError: inclusive_range off-by-one

FAILED test_ranges.py::test_inclusive_range_basic
assert [1, 2] == [1, 2, 3]

File: ranges.py — closed interval should include end.
""",
    "case_007": """AttributeError: 'NoneType' object has no attribute 'display_name'

  File "users.py", line 18, in display_name
    return user.profile.display_name
AttributeError: 'NoneType' object has no attribute 'display_name'

Failed: test_users.py::test_display_name_fallback_to_name
""",
    "case_008": """AssertionError: average_percent scales scores incorrectly

FAILED test_report.py::test_average_percent
assert 0.85 == 85.0

Call chain: report.average_percent → transform.normalize_score → values.clamp
Scores are already 0–100; normalize_score should not divide by 100.
""",
    "case_009": """KeyError: 'tool' — missing [tool.eval] in pyproject.toml

  File "config_loader.py", line 12, in load_multiplier
    return int(data["tool"]["eval"]["multiplier"])
KeyError: 'tool'

Failed: test_config.py::test_multiplier
Fix: add [tool.eval] multiplier = 2 to pyproject.toml
""",
    "case_010": """ModuleNotFoundError + TypeError (composite, two files)

  File "gateway.py", line 3, in <module>
    from backend.service import run_task
ModuleNotFoundError: No module named 'backend.service'

After import fix, run_task("2", 3) raises TypeError on str + int.
Failed: test_gateway.py::test_invoke_mixed_types
""",
}

MIN_LINES: dict[str, int] = {
    "case_001": 1,
    "case_002": 1,
    "case_003": 2,
    "case_004": 1,
    "case_005": 2,
    "case_006": 1,
    "case_007": 3,
    "case_008": 1,
    "case_009": 2,
    "case_010": 2,
}

SOURCE_FILES: dict[str, list[str]] = {
    "case_001": ["pricing.py"],
    "case_002": ["labels.py"],
    "case_003": ["concat.py"],
    "case_004": ["app.py"],
    "case_005": ["service.py"],
    "case_006": ["ranges.py"],
    "case_007": ["users.py"],
    "case_008": ["transform.py"],
    "case_009": ["pyproject.toml"],
    "case_010": ["gateway.py", "backend/tasks.py"],
}


def unified_diff(path: Path, old: str, new: str) -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    rel = path.as_posix()
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )


def run_pytest(repo: Path) -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return proc.returncode


def main() -> int:
    errors: list[str] = []
    for case_id, file_fixes in FIXES.items():
        case_dir = CASES_DIR / case_id
        repo = case_dir / "repo"
        if not repo.is_dir():
            errors.append(f"{case_id}: missing repo")
            continue

        diff_parts: list[str] = []
        for rel, fixed in file_fixes.items():
            src = repo / rel
            old = src.read_text(encoding="utf-8")
            if old == fixed:
                errors.append(f"{case_id}: {rel} already fixed in repo")
                continue
            diff_parts.append(unified_diff(Path(rel), old, fixed))

        patch_text = "\n".join(p.strip("\n") for p in diff_parts if p.strip())
        (case_dir / "expected_patch.diff").write_text(patch_text + "\n", encoding="utf-8")
        (case_dir / "issue.txt").write_text(ISSUES[case_id].strip() + "\n", encoding="utf-8")
        (case_dir / "min_lines.txt").write_text(f"{MIN_LINES[case_id]}\n", encoding="utf-8")

        # Verify: copy repo, apply fixes, pytest green
        with tempfile.TemporaryDirectory() as tmp:
            tmp_repo = Path(tmp) / "repo"
            shutil.copytree(repo, tmp_repo)
            for rel, fixed in file_fixes.items():
                (tmp_repo / rel).write_text(fixed, encoding="utf-8")
            if run_pytest(tmp_repo) != 0:
                errors.append(f"{case_id}: pytest still fails after fix")

        # Verify: buggy repo still fails
        if run_pytest(repo) == 0:
            errors.append(f"{case_id}: buggy repo should fail pytest")

        # Update metadata.yaml status
        meta_path = case_dir / "metadata.yaml"
        meta = meta_path.read_text(encoding="utf-8")
        meta = meta.replace("status: scaffolded", "status: verified")
        if "source_files: []" in meta:
            files = SOURCE_FILES[case_id]
            meta = meta.replace(
                "source_files: []",
                "source_files:\n" + "\n".join(f"  - {f}" for f in files),
            )
            tests = list(repo.glob("test_*.py"))
            meta = meta.replace(
                "test_files: []",
                "test_files:\n" + "\n".join(f"  - {t.name}" for t in tests),
            )
        meta_path.write_text(meta, encoding="utf-8")
        print(f"OK {case_id}")

    if errors:
        print("ERRORS:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

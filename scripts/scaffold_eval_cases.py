"""一次性脚本：生成 M7 eval case 目录骨架（M7D1 任务 1）。

说明：输出文件中的 `# TODO(M7D1)` 为脚手架占位符；正式 Case 已手工填充，脚本仅保留供参考。
"""

from pathlib import Path

CASES = [
    ("case_001", "type_error", "easy", 25, "二元运算参数类型未转换（str + int）", ["type_error", "coercion"]),
    ("case_002", "type_error", "medium", 35, "返回值类型与调用方期望不符", ["type_error", "return_type"]),
    ("case_003", "type_error", "medium", 35, "未判空导致对 None 运算或拼接", ["type_error", "none_guard"]),
    ("case_004", "import_error", "medium", 40, "模块路径错误或缺少 __init__.py", ["import_error", "package_layout"]),
    ("case_005", "import_error", "medium", 40, "错误 import 名或子模块路径", ["import_error", "wrong_symbol"]),
    ("case_006", "logic_error", "medium", 35, "off-by-one 边界（循环或切片）", ["logic_error", "off_by_one"]),
    ("case_007", "attribute_error", "medium", 40, "对 None 或未初始化对象取属性", ["attribute_error", "none_deref"]),
    ("case_008", "logic_error", "hard", 60, "多跳调用链（>=3 hop）中的逻辑缺陷", ["logic_error", "multi_hop"]),
    ("case_009", "config_error", "hard", 50, "pyproject.toml 依赖声明缺失致构建失败", ["config_error", "pyproject"]),
    ("case_010", "composite", "hard", 75, "跨两文件：类型错误与导入错误组合", ["composite", "multi_file"]),
]

root = Path(__file__).resolve().parents[1] / "src" / "eval" / "cases"
for case_id, issue_type, difficulty, est, desc, tags in CASES:
    d = root / case_id
    (d / "repo").mkdir(parents=True, exist_ok=True)
    tags_yaml = "\n".join(f"  - {t}" for t in tags)
    (d / "metadata.yaml").write_text(
        f"case_id: {case_id}\n"
        f"language: python\n"
        f"issue_type: {issue_type}\n"
        f"difficulty: {difficulty}\n"
        f"status: scaffolded\n"
        f"estimated_duration_s: {est}\n"
        f'description: "{desc}"\n'
        f"tags:\n{tags_yaml}\n"
        f"source_files: []\n"
        f"test_files: []\n",
        encoding="utf-8",
    )
    (d / "issue.txt").write_text(
        f"# TODO(M7D1): 待填充 — {desc}\n"
        f"# 格式：GitHub Issue 标题 + 堆栈 / CI 日志摘录\n",
        encoding="utf-8",
    )
    (d / "expected_patch.diff").write_text(
        f"# TODO(M7D1): 人工标注的最小正确修复（unified diff）\n"
        f"# Case: {case_id} — {desc}\n",
        encoding="utf-8",
    )
    (d / "min_lines.txt").write_text("TBD\n", encoding="utf-8")
    (d / "repo" / ".gitkeep").write_text("", encoding="utf-8")

print(f"created {len(CASES)} cases under {root}")

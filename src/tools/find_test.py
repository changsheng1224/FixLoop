"""find_test Tool：启发式搜索定位函数的对应测试。

策略：
1. 同目录或父目录下 tests/ 中文件名匹配 test_<module>.py
2. 搜索 def test_*{func_name}* 在测试文件中
3. 搜索 import.*{module} 在测试文件中
"""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FindTestArgs:
    function_name: str  # 必填
    file_path: str  # 必填


def find_test_for_function(context, args: dict) -> str:
    """查找函数的对应测试。

    Args:
        context: ToolContext 实例。
        args: 包含 'function_name' 和 'file_path' 的字典。
    """
    func_name = args.get("function_name", "")
    file_path = args.get("file_path", "")
    if not func_name or not file_path:
        return "Error: 缺少必填参数 function_name 或 file_path"

    try:
        target = context.resolve(file_path)
    except ValueError as e:
        return f"Error: {e}"

    module_name = target.stem  # calculator.py → calculator
    results = []

    # 策略 1：同目录 tests/ 下文件名匹配
    project_root = Path(context.root)
    test_dirs = list(project_root.rglob("tests")) + list(project_root.rglob("test"))
    for test_dir in test_dirs:
        test_file = test_dir / f"test_{module_name}.py"
        if test_file.exists():
            results.append(
                {
                    "test_file": str(test_file.relative_to(project_root)),
                    "confidence": 0.9,
                    "strategy": "filename_match",
                }
            )

    # 策略 2：搜索测试函数名
    for test_dir in test_dirs:
        for test_file in test_dir.glob("test_*.py"):
            try:
                content = test_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            import re

            pattern = rf"def test_\w*{re.escape(func_name)}\w*"
            matches = re.findall(pattern, content)
            for m in matches:
                results.append(
                    {
                        "test_file": str(test_file.relative_to(project_root)),
                        "test_function": m.replace("def ", ""),
                        "confidence": 0.7,
                        "strategy": "function_name_match",
                    }
                )

    if not results:
        return "(未找到对应测试)"

    # 去重按 confidence 降序
    seen = set()
    unique = []
    for r in sorted(results, key=lambda x: x["confidence"], reverse=True):
        key = (r["test_file"], r.get("test_function", ""))
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return json.dumps(unique, ensure_ascii=False, indent=2)

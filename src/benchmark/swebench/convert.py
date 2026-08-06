"""Issue 文本到 FixLoop repair 输入。"""



from __future__ import annotations

import re
from pathlib import Path

from src.benchmark.swebench.types import SweInstance

_FAIL_TO_PASS_HEADER = "FAIL_TO_PASS tests (hints, may not exist locally):"

_FAIL_TO_PASS_LINE = re.compile(r"^\s*-\s+(\S.+?)\s*$")

# unittest 风格：test_foo (pkg.mod.ClassName)

_UNITTEST_STYLE = re.compile(

    r"^(?P<method>[A-Za-z_]\w*)\s+\((?P<qual>[\w.]+)\)\s*$"

)





def instance_to_issue(instance: SweInstance) -> str:

    """将 SWE-bench problem_statement 转为 FixLoop ``repair(issue)`` 文本。"""

    parts = [

        f"[SWE-bench] instance_id={instance.instance_id}",

        f"repo={instance.repo}",

        f"base_commit={instance.base_commit}",

        "",

        instance.problem_statement.strip() or "(empty problem_statement)",

    ]

    if instance.FAIL_TO_PASS:

        parts.append("")

        parts.append(_FAIL_TO_PASS_HEADER)

        for t in instance.FAIL_TO_PASS[:20]:

            parts.append(f"- {t}")

    return "\n".join(parts)





def extract_fail_to_pass_hints(issue: str) -> list[str]:
    """从 ``instance_to_issue`` 文本解析 FAIL_TO_PASS 提示（通用，不绑 instance_id）。

    供 Retriever 降级与 Verifier ``test_path`` 选择使用，避免空路径跑全仓收集 0 tests。
    """
    from src.repair.localization.fail_to_pass_hints import extract_fail_to_pass_hints as _extract

    return _extract(issue)





def _looks_like_class(name: str) -> bool:

    return bool(name) and name[0].isupper() and "_" not in name[:1]





def _find_test_file(repo: Path, module_parts: list[str]) -> Path | None:

    """在仓库中定位测试模块文件（相对 repo）。"""

    if not module_parts:

        return None

    candidates = [

        Path(*module_parts).with_suffix(".py"),

        Path("tests", *module_parts).with_suffix(".py"),

        Path("test", *module_parts).with_suffix(".py"),

    ]

    # auth_tests.test_validators → tests/auth_tests/test_validators.py

    if not str(candidates[0]).startswith("tests"):

        candidates.append(Path("tests", *module_parts).with_suffix(".py"))

    for rel in candidates:

        if (repo / rel).is_file():

            return rel

    # 按 basename 搜索

    basename = module_parts[-1] + ".py"

    hits = [p for p in repo.rglob(basename) if p.is_file()]

    if not hits:

        return None

    # 偏好含 module 路径片段的命中

    scored: list[tuple[int, Path]] = []

    needle = "/".join(module_parts[:-1]) if len(module_parts) > 1 else module_parts[0]

    for hit in hits:

        try:

            rel = hit.relative_to(repo)

        except ValueError:

            continue

        score = 0

        s = str(rel).replace("\\", "/")

        if needle and needle.replace(".", "/") in s:

            score += 10

        if "test" in s:

            score += 1

        scored.append((score, rel))

    if not scored:

        return None

    scored.sort(key=lambda x: (-x[0], len(str(x[1]))))

    return scored[0][1]





def resolve_test_ref_for_pytest(ref: str, repo_root: str | Path | None = None) -> str:

    """将 FAIL_TO_PASS / related_tests 条目尽量转为 pytest 可收集 target（E17′）。



    支持：

    - 已是 ``path::node`` → 校验/重定位文件

    - ``test_x (pkg.mod.Class)`` unittest 风格 → ``path::Class::test_x``

    - 裸名 → 尽力 rglob；失败则原样返回

    """

    raw = (ref or "").strip()

    if not raw:

        return ""

    repo = Path(repo_root).resolve() if repo_root else None



    m = _UNITTEST_STYLE.match(raw)

    if m:

        method = m.group("method")

        parts = m.group("qual").split(".")

        class_name = ""

        module_parts = parts

        if parts and _looks_like_class(parts[-1]):

            class_name = parts[-1]

            module_parts = parts[:-1]

        file_rel: Path | None = None

        if repo is not None and repo.is_dir():

            file_rel = _find_test_file(repo, module_parts)

        if file_rel is not None:

            node = str(file_rel).replace("\\", "/")

            if class_name:

                return f"{node}::{class_name}::{method}"

            return f"{node}::{method}"

        # 无仓时仍给出可识别的 pytest 风格，供日志区分

        mod = "/".join(module_parts) + ".py"

        if class_name:

            return f"{mod}::{class_name}::{method}"

        return f"{mod}::{method}"



    if "::" in raw:

        file_part, _, rest = raw.partition("::")

        file_part = file_part.strip().replace("\\", "/")

        if repo is not None and repo.is_dir():

            direct = repo / file_part

            if direct.is_file():

                return f"{file_part}::{rest}" if rest else file_part

            found = _find_test_file(repo, Path(file_part).with_suffix("").parts)

            if found is None:

                # basename search

                hits = list(repo.rglob(Path(file_part).name))

                if hits:

                    try:

                        found = hits[0].relative_to(repo)

                    except ValueError:

                        found = None

            if found is not None:

                node = str(found).replace("\\", "/")

                return f"{node}::{rest}" if rest else node

        return raw



    # 裸测试名

    if repo is not None and repo.is_dir() and raw.isidentifier():

        # 不盲目全仓 -k；留给调用方。若唯一 test_*.py 含 def raw 可定位——成本高，跳过。

        pass

    return raw





def normalize_related_test_refs(

    refs: list[str], repo_root: str | Path | None = None

) -> list[str]:

    """批量规范化 related_tests / FAIL_TO_PASS，去重保序。"""

    out: list[str] = []

    seen: set[str] = set()

    for ref in refs:

        if not isinstance(ref, str) or not ref.strip():

            continue

        norm = resolve_test_ref_for_pytest(ref.strip(), repo_root)

        if norm and norm not in seen:

            seen.add(norm)

            out.append(norm)

    return out



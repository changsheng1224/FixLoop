"""短修快路径：单上下文加深（少角色切换、多工具读改）。

当已有明确失败 nodeid + 少量实现嫌疑时：
- 预载测试/实现片段进同一 prompt
- 提高 patcher max_steps，避免过早被步数截断
- 收窄嫌疑列表，禁止本回合扩搜无关目录

能力向（非绑题号）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.state import RepairState, SuspectLocation

__all__ = [
    "ShortRepairDecision",
    "build_workspace_brief",
    "detect_short_repair",
    "filter_suspects_for_short_repair",
    "pop_patcher_depth",
    "push_patcher_depth",
]

_TEST_HINTS = ("/tests/", "\\tests\\", "/test/", "test_", "_test.py")


def _is_test_path(path: str) -> bool:
    p = (path or "").replace("\\", "/")
    base = Path(p).name
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    lowered = f"/{p.lower()}/"
    return any(h.replace("\\", "/") in lowered for h in ("/tests/", "/test/", "/testing/"))


def _impl_suspects(suspects: list["SuspectLocation"] | None) -> list["SuspectLocation"]:
    out: list = []
    seen: set[str] = set()
    for s in suspects or []:
        fp = str(getattr(s, "file_path", "") or "").replace("\\", "/")
        if not fp or _is_test_path(fp) or fp in seen:
            continue
        seen.add(fp)
        out.append(s)
    return out


@dataclass
class ShortRepairDecision:
    enabled: bool = False
    reason: str = ""
    impl_files: list[str] = field(default_factory=list)
    test_nodeids: list[str] = field(default_factory=list)
    max_steps: int = 16

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "impl_files": list(self.impl_files),
            "test_nodeids": list(self.test_nodeids),
            "max_steps": self.max_steps,
        }


def detect_short_repair(
    state: "RepairState",
    repo_root: str | Path = "",
    *,
    max_impl_files: int = 3,
) -> ShortRepairDecision:
    """判断是否进入短修快路径。"""
    bucket = str(state.node_timings.get("verify_bucket") or "").strip().lower()
    if bucket in ("env", "collect"):
        return ShortRepairDecision(enabled=False, reason="blocked_by_bucket")

    from src.repair.fail_surface import preferred_verify_targets

    nodeids = [n for n in preferred_verify_targets(state) if n][:5]
    related: list[str] = []
    ctx = getattr(state, "retrieved_context", None)
    if ctx is not None:
        related = [str(x) for x in (ctx.related_tests or []) if x][:5]

    test_anchors = nodeids or [t for t in related if "::" in t or t.endswith(".py")]
    suspects = _impl_suspects(getattr(state, "suspect_locations", None))
    if not suspects:
        plan = getattr(state, "repair_plan", None)
        files = list(getattr(plan, "suspect_files", None) or []) if plan else []
        from src.state import SuspectLocation

        for f in files:
            fp = str(f).replace("\\", "/")
            if fp and not _is_test_path(fp):
                suspects.append(
                    SuspectLocation(
                        file_path=fp,
                        start_line=1,
                        end_line=1,
                        reason="plan",
                        confidence=0.6,
                    )
                )

    root = Path(repo_root) if repo_root else None
    if root is not None:
        suspects = [
            s
            for s in suspects
            if (root / str(s.file_path).replace("\\", "/")).is_file()
        ]

    if not suspects:
        return ShortRepairDecision(enabled=False, reason="no_impl_suspect")

    # 定位门禁要求短修：即使略弥散也强制收敛
    force = bool(state.node_timings.get("force_short_repair"))

    # P1：单一高置信实现文件 → 直接短修（少轮次、少 timeout 清零）
    if len(suspects) == 1:
        conf = float(getattr(suspects[0], "confidence", 0) or 0)
        reason_s = str(getattr(suspects[0], "reason", "") or "")
        if conf >= 0.7 or reason_s in (
            "堆栈指向",
            "F2P覆盖",
            "test_patch覆盖",
            "issue 路径",
            "localize_confirmed",
            "faithfulness_promoted",
            "faithfulness_soft",
        ):
            force = True

    # 首轮无测试锚点时：仅当实现嫌疑很少才短修（强制短修除外）
    if not force and not test_anchors and len(suspects) > 2:
        return ShortRepairDecision(enabled=False, reason="too_diffuse")

    impl_files = [str(s.file_path).replace("\\", "/") for s in suspects[:max_impl_files]]
    if force:
        reason = "force_mid_tier" if state.node_timings.get("force_short_repair") else "single_impl"
    else:
        reason = "fail_surface" if nodeids else ("related_tests" if test_anchors else "few_suspects")
    # 有失败面时给更多工具步；首轮略少
    steps = 18 if nodeids or force else 14
    return ShortRepairDecision(
        enabled=True,
        reason=reason,
        impl_files=impl_files,
        test_nodeids=list(nodeids or test_anchors)[:3],
        max_steps=steps,
    )


def filter_suspects_for_short_repair(
    suspects: list["SuspectLocation"] | None,
    decision: ShortRepairDecision,
) -> list["SuspectLocation"]:
    if not decision.enabled or not decision.impl_files:
        return list(suspects or [])
    allow = set(decision.impl_files)
    kept = [
        s
        for s in (suspects or [])
        if str(getattr(s, "file_path", "") or "").replace("\\", "/") in allow
    ]
    return kept or list(suspects or [])


def _read_window(path: Path, *, start_line: int = 1, max_lines: int = 80) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if not lines:
        return ""
    start = max(1, int(start_line or 1)) - 1
    end = min(len(lines), start + max_lines)
    start = max(0, min(start, len(lines) - 1))
    chunk = lines[start:end]
    numbered = [f"{start + i + 1:>4}|{row}" for i, row in enumerate(chunk)]
    return "\n".join(numbered)


def build_workspace_brief(
    decision: ShortRepairDecision,
    state: "RepairState",
    repo_root: str | Path,
    *,
    max_chars: int = 7000,
) -> str:
    """把测试+实现预载进同一上下文，减少「先找再改」的角色切换。"""
    if not decision.enabled:
        return ""
    root = Path(repo_root)
    lines = [
        "[短修快路径 SHORT REPAIR]",
        "本回合在同一上下文连续完成：read_file 失败测试 → read_file 实现 → patch_file 最小改动。",
        "禁止：切换到无关目录扩搜、大范围重构、重复提交相同 hunk。",
        f"原因: {decision.reason}; 工具步预算≈{decision.max_steps}",
    ]
    if decision.test_nodeids:
        lines.append("锚定测试:")
        for n in decision.test_nodeids:
            lines.append(f"  - {n}")
        from src.repair.fail_surface import read_test_function_excerpt

        for nid in decision.test_nodeids[:2]:
            excerpt = read_test_function_excerpt(root, nid)
            if excerpt:
                lines.append(f"测试原文 ({nid}):")
                lines.append("```python")
                lines.append(excerpt[:1800])
                lines.append("```")

    lines.append("锚定实现:")
    by_path = {
        str(getattr(s, "file_path", "") or "").replace("\\", "/"): s
        for s in (state.suspect_locations or [])
    }
    for fp in decision.impl_files[:3]:
        lines.append(f"  - {fp}")
        path = root / fp
        if not path.is_file():
            continue
        start = 1
        hit = by_path.get(fp)
        if hit is not None:
            start = int(getattr(hit, "start_line", 1) or 1)
        window = _read_window(path, start_line=max(1, start - 10), max_lines=70)
        if window:
            lines.append(f"实现摘录 ({fp}@{start}):")
            lines.append("```python")
            lines.append(window)
            lines.append("```")

    lines.append(
        "动作顺序: 1) 对照上方测试断言 2) 在锚定实现内 patch_file "
        "3) 不要输出大段无关 JSON；工具改盘即可。"
    )
    text = "\n".join(lines)
    return text[:max_chars]


@dataclass
class _DepthToken:
    prev_max_steps: int


def push_patcher_depth(agent, max_steps: int) -> _DepthToken | None:
    """临时设定 patcher 工具步数（可升可降）；返回 token 供 pop 还原。"""
    if agent is None or max_steps <= 0:
        return None
    cfg = getattr(agent, "config", None)
    if cfg is None or not hasattr(cfg, "max_steps"):
        return None
    prev = int(cfg.max_steps or 0)
    cfg.max_steps = int(max_steps)
    return _DepthToken(prev_max_steps=prev)


def pop_patcher_depth(agent, token: _DepthToken | None) -> None:
    if agent is None or token is None:
        return
    cfg = getattr(agent, "config", None)
    if cfg is None or not hasattr(cfg, "max_steps"):
        return
    cfg.max_steps = int(token.prev_max_steps)

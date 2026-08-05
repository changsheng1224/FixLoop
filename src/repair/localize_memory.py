"""跨 retry 的 localize 记忆：确认实现 / 烧毁文件。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.state import RepairState, SuspectLocation

__all__ = [
    "apply_localize_memory",
    "load_localize_memory",
    "remember_confirmed_impls",
    "remember_negated_files",
    "save_localize_memory",
]

_KEY = "localize_memory"


def load_localize_memory(state: "RepairState") -> dict[str, Any]:
    raw = state.node_timings.get(_KEY)
    if not isinstance(raw, dict):
        return {"burned_files": [], "confirmed_impls": []}
    burned = [str(x).replace("\\", "/") for x in (raw.get("burned_files") or []) if x]
    confirmed = []
    for item in raw.get("confirmed_impls") or []:
        if isinstance(item, dict) and item.get("file_path"):
            confirmed.append(
                {
                    "file_path": str(item["file_path"]).replace("\\", "/"),
                    "start_line": int(item.get("start_line") or 1),
                    "function_name": item.get("function_name"),
                    "reason": item.get("reason") or "localize_confirmed",
                    "confidence": float(item.get("confidence") or 0.9),
                }
            )
    return {"burned_files": burned, "confirmed_impls": confirmed}


def save_localize_memory(state: "RepairState", memory: dict[str, Any]) -> None:
    state.node_timings[_KEY] = {
        "burned_files": list(dict.fromkeys(memory.get("burned_files") or [])),
        "confirmed_impls": list(memory.get("confirmed_impls") or [])[:12],
    }


def remember_negated_files(state: "RepairState") -> None:
    """从 failure_ledger 否定文件写入 burned。"""
    mem = load_localize_memory(state)
    burned = list(mem["burned_files"])
    ledger = state.node_timings.get("failure_ledger") or {}
    if isinstance(ledger, dict):
        for f in ledger.get("negated_files") or []:
            fp = str(f).replace("\\", "/")
            if fp and fp not in burned:
                burned.append(fp)
    # 也吸收 agent_errors 里明确失败的路径
    mem["burned_files"] = burned
    save_localize_memory(state, mem)


def remember_confirmed_impls(
    state: "RepairState",
    suspects: list["SuspectLocation"] | None,
    *,
    max_keep: int = 6,
    repo_root: str = "",
) -> None:
    """把 HIGH/MID 实现记为 confirmed，供下一轮 seed。"""
    from src.repair.localize_tiers import SuspectTier, tier_for_suspect

    mem = load_localize_memory(state)
    confirmed = list(mem["confirmed_impls"])
    seen = {c["file_path"] for c in confirmed}
    root = (
        repo_root
        or str(state.node_timings.get("_repo_root_hint") or "")
        or ""
    )
    for s in suspects or []:
        fp = str(getattr(s, "file_path", "") or "").replace("\\", "/")
        if not fp or fp in seen:
            continue
        reason = (getattr(s, "reason", "") or "").strip()
        if root:
            tier = tier_for_suspect(s, root)
        elif reason in ("堆栈指向", "F2P覆盖", "test_patch覆盖", "issue 路径"):
            tier = SuspectTier.HIGH
        elif reason in ("grep命中", "测试覆盖边", "语义扩展", "issue 符号"):
            tier = SuspectTier.MID
        else:
            tier = SuspectTier.LOW
        if tier not in (SuspectTier.HIGH, SuspectTier.MID):
            continue
        confirmed.append(
            {
                "file_path": fp,
                "start_line": int(getattr(s, "start_line", 1) or 1),
                "function_name": getattr(s, "function_name", None),
                "reason": "localize_confirmed",
                "confidence": max(0.9, float(getattr(s, "confidence", 0) or 0)),
            }
        )
        seen.add(fp)
        if len(confirmed) >= max_keep:
            break
    mem["confirmed_impls"] = confirmed[:max_keep]
    save_localize_memory(state, mem)


def apply_localize_memory(
    suspects: list["SuspectLocation"] | None,
    state: "RepairState",
) -> list["SuspectLocation"]:
    """注入 confirmed，过滤 burned。"""
    from src.state import SuspectLocation

    mem = load_localize_memory(state)
    burned = set(mem["burned_files"])
    out: list[SuspectLocation] = []
    seen: set[str] = set()

    for item in mem["confirmed_impls"]:
        fp = item["file_path"]
        if fp in burned or fp in seen:
            continue
        seen.add(fp)
        out.append(
            SuspectLocation(
                file_path=fp,
                start_line=int(item.get("start_line") or 1),
                end_line=int(item.get("start_line") or 1),
                function_name=item.get("function_name"),
                reason=str(item.get("reason") or "localize_confirmed"),
                confidence=float(item.get("confidence") or 0.9),
            )
        )

    for s in suspects or []:
        fp = str(getattr(s, "file_path", "") or "").replace("\\", "/")
        if not fp or fp in burned:
            continue
        if fp in seen:
            continue
        seen.add(fp)
        out.append(s)
    return out

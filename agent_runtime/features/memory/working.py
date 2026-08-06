"""工作记忆 — Working Memory：当前任务 + 最近文件 + 文件摘要 + 证据账本。"""

import hashlib
import time
from pathlib import Path

from agent_runtime.features.memory.core import MAX_EVIDENCE_ENTRIES, MAX_RECENT_FILES
from agent_runtime.repair_context import get_repair_context


def set_task_summary(state: dict, user_message: str):
    """将用户请求截断写入 working.task_summary。"""
    summary = user_message.strip().replace("\n", " ")[:300]
    state["working"]["task_summary"] = summary
    get_repair_context(state)["goal"] = summary


def remember_file(state: dict, path: str):
    """将 path 加入 recent_files LRU（last_access 时间戳决定淘汰）。"""
    files = state["working"]["recent_files"]
    meta = state["working"].setdefault("_recent_files_meta", {})
    now = time.time()
    if path in files:
        files.remove(path)
    files.append(path)
    state["working"]["recent_files"] = files[-MAX_RECENT_FILES:]
    # last_access 时间戳
    meta[path] = {"last_access": now, "added_at": meta.get(path, {}).get("added_at", now)}
    for stale in list(meta.keys()):
        if stale not in state["working"]["recent_files"]:
            del meta[stale]


def _read_fingerprint(path: str, start: int, end: int, result_text: str) -> str:
    body = "\n".join(
        [
            (path or "").strip().replace("\\", "/"),
            str(max(1, int(start or 1))),
            str(max(1, int(end or 1))),
            (result_text or "").strip()[:1200],
        ]
    )
    return hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:16]


def _evidence_cache(state: dict) -> tuple[list[dict], dict[str, int]]:
    working = state.setdefault("working", {})
    ledger = working.setdefault("evidence_ledger", [])
    cache = working.setdefault("read_cache", {})
    if not isinstance(ledger, list):
        ledger = []
        working["evidence_ledger"] = ledger
    if not isinstance(cache, dict):
        cache = {}
        working["read_cache"] = cache
    return ledger, cache


def _trim_evidence_ledger(state: dict) -> None:
    ledger, cache = _evidence_cache(state)
    if len(ledger) <= MAX_EVIDENCE_ENTRIES:
        return
    kept = ledger[-MAX_EVIDENCE_ENTRIES:]
    working = state["working"]
    working["evidence_ledger"] = kept
    working["read_cache"] = {}
    for idx, item in enumerate(kept):
        fp = str(item.get("content_hash") or "")
        if fp:
            working["read_cache"][fp] = idx


def record_read_evidence(
    state: dict,
    *,
    path: str,
    start: int,
    end: int,
    result_text: str,
) -> dict:
    """记录一次 read_file 证据；重复窗口 + 相同内容只增加计数。"""
    ledger, cache = _evidence_cache(state)
    fp = _read_fingerprint(path, start, end, result_text)
    now = time.time()
    if fp in cache:
        idx = cache[fp]
        if 0 <= idx < len(ledger):
            entry = ledger[idx]
            entry["duplicate_count"] = int(entry.get("duplicate_count", 0)) + 1
            entry["last_seen_at"] = now
            return {"duplicate": True, **entry}

    entry = {
        "id": f"E{len(ledger) + 1}",
        "kind": "read_file",
        "path": path,
        "start": int(start or 1),
        "end": int(end or 1),
        "content_hash": fp,
        "summary": (result_text or "").strip()[:180],
        "duplicate_count": 0,
        "created_at": now,
        "last_seen_at": now,
        "stale": False,
        "status": "live",
        "confidence": 0.7,
        "content_excerpt": (result_text or "")[:4000],
    }
    ledger.append(entry)
    get_repair_context(state)["evidence_refs"].append(entry["id"])
    cache[fp] = len(ledger) - 1
    _trim_evidence_ledger(state)
    return {"duplicate": False, **entry}


def record_search_evidence(
    state: dict,
    *,
    pattern: str,
    path: str,
    result_text: str,
) -> dict:
    """记录一次 search 证据。"""
    ledger, cache = _evidence_cache(state)
    fp = _read_fingerprint(f"search::{path}::{pattern}", 1, 1, result_text)
    now = time.time()
    if fp in cache:
        idx = cache[fp]
        if 0 <= idx < len(ledger):
            entry = ledger[idx]
            entry["duplicate_count"] = int(entry.get("duplicate_count", 0)) + 1
            entry["last_seen_at"] = now
            return {"duplicate": True, **entry}

    entry = {
        "id": f"E{len(ledger) + 1}",
        "kind": "search",
        "path": path,
        "pattern": pattern,
        "content_hash": fp,
        "summary": (result_text or "").strip()[:180],
        "duplicate_count": 0,
        "created_at": now,
        "last_seen_at": now,
        "stale": False,
        "status": "live",
        "confidence": 0.6,
        "content_excerpt": (result_text or "")[:4000],
    }
    ledger.append(entry)
    get_repair_context(state)["evidence_refs"].append(entry["id"])
    cache[fp] = len(ledger) - 1
    _trim_evidence_ledger(state)
    return {"duplicate": False, **entry}


def render_evidence_ledger(state: dict, *, max_entries: int = 4) -> str:
    """将证据账本压成短块，供 prompt / trace 使用。"""
    working = state.get("working", {}) if isinstance(state, dict) else {}
    ledger = working.get("evidence_ledger", []) if isinstance(working, dict) else []
    if not ledger:
        return ""

    tail = ledger[-max_entries:]
    dup_total = sum(int(item.get("duplicate_count", 0) or 0) for item in tail)
    stale_total = sum(1 for item in tail if item.get("stale"))
    lines = ["证据账本:"]
    lines.append(f"  最近{len(tail)}条; 重复={dup_total}; 过期={stale_total}")
    for item in tail:
        kind = str(item.get("kind") or "read")
        path = str(item.get("path") or "")
        start = int(item.get("start", 1) or 1)
        end = int(item.get("end", start) or start)
        ref = str(item.get("id") or "E?")
        fp = str(item.get("content_hash") or "")[:8]
        dup = int(item.get("duplicate_count", 0) or 0)
        stale = "stale" if item.get("stale") else "live"
        summary = str(item.get("summary") or "").strip().replace("\n", " ")[:72]
        if kind == "search":
            pattern = str(item.get("pattern") or "")
            lines.append(
                f"  - {ref} search {pattern!r} @ {path} hash={fp} dup={dup} {stale}: {summary}"
            )
        else:
            lines.append(
                f"  - {ref} read {path}:{start}-{end} hash={fp} dup={dup} {stale}: {summary}"
            )
    return "\n".join(lines)


def set_file_summary(state: dict, path: str, summary: str):
    """缓存文件摘要及 freshness hash。"""
    state["file_summaries"][path] = {
        "summary": summary[:180],
        "created_at": time.time(),
        "freshness": _freshness_hash(path),
        "content_hash": _freshness_hash(path),
        "scope": "run",
    }


def invalidate_file_summary(state: dict, path: str):
    """文件变更后清除过期摘要。"""
    state["file_summaries"].pop(path, None)
    ledger, cache = _evidence_cache(state)
    changed = False
    for idx, entry in enumerate(ledger):
        if str(entry.get("path") or "") == path:
            entry["stale"] = True
            entry["status"] = "stale"
            entry["last_seen_at"] = time.time()
            changed = True
    if changed:
        for fp, idx in list(cache.items()):
            if 0 <= idx < len(ledger) and str(ledger[idx].get("path") or "") == path:
                del cache[fp]


def _freshness_hash(path: str) -> str:
    try:
        p = Path(path)
        if p.exists():
            stat = p.stat()
            return f"{stat.st_mtime}:{stat.st_size}"
    except OSError:
        pass
    return ""


def expand_evidence(state: dict, ref_id: str, *, max_chars: int = 4000) -> dict:
    """Resolve a compacted evidence reference from the in-memory ledger."""
    ledger = state.get("working", {}).get("evidence_ledger", [])
    for entry in ledger if isinstance(ledger, list) else []:
        if str(entry.get("id")) == str(ref_id):
            return {
                "found": True,
                "id": ref_id,
                "status": "stale" if entry.get("stale") else entry.get("status", "live"),
                "path": entry.get("path", ""),
                "start": entry.get("start", 1),
                "end": entry.get("end", 1),
                "content_hash": entry.get("content_hash", ""),
                "content": str(entry.get("content_excerpt", ""))[:max_chars],
            }
    return {"found": False, "id": ref_id}

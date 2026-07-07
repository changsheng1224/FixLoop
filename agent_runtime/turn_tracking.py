"""Session turn 边界追踪：为 history 条目打 turn_id，供 L0–L4 保护当前轮。"""

from __future__ import annotations


def current_turn_id(session: dict) -> int | None:
    """当前进行中的 turn_id；尚无 user 消息时返回 None。"""
    counter = int(session.get("_turn_counter", 0) or 0)
    return counter if counter > 0 else None


def stamp_turn_id(session: dict, item: dict) -> dict:
    """为 history 条目写入 turn_id；user 消息开启新 turn。"""
    stamped = dict(item)
    counter = int(session.get("_turn_counter", 0) or 0)
    if stamped.get("role") == "user":
        counter += 1
        session["_turn_counter"] = counter
    if counter > 0:
        stamped.setdefault("turn_id", counter)
    return stamped


def turn_id_of_turn(turn: list[dict]) -> int | None:
    """从 turn 分组中取 turn_id（取首条非空）。"""
    for item in turn:
        tid = item.get("turn_id")
        if tid is not None:
            return int(tid)
    return None


def is_current_turn(turn: list[dict], active_turn_id: int | None) -> bool:
    if active_turn_id is None:
        return False
    tid = turn_id_of_turn(turn)
    return tid is not None and tid == active_turn_id

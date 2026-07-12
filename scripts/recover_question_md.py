#!/usr/bin/env python3
"""从字符级损坏的 question.md 恢复（每字符一行）。"""

from pathlib import Path

path = Path(__file__).resolve().parents[1] / "docs" / "question.md"
raw = path.read_text(encoding="utf-8")
lines = raw.splitlines()

out: list[str] = []
buf: list[str] = []

def flush_buf() -> None:
    global buf
    if buf:
        out.append("".join(buf))
        buf = []


for line in lines:
    s = line.rstrip("\r")
    if s == "":
        flush_buf()
        out.append("")
        continue
    # 单字符行（损坏特征）并入 buffer
    if len(s) == 1 or (len(s) == 2 and s.startswith(" ")):
        buf.append(s.strip() if s.strip() else s)
        continue
    flush_buf()
    out.append(s)

flush_buf()
restored = "\n".join(out)
path.write_text(restored, encoding="utf-8")
print(f"Restored {path} ({len(restored)} chars, {len(lines)} raw lines)")

"""Renumber all questions in docs/agent.md sequentially (1..N)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_MD = ROOT / "docs" / "agent.md"

NUM_PREFIX = re.compile(r"^\d+\.?\s*")


def renumber_line(line: str, n: int) -> str:
    s = line.strip()
    bold = s.startswith("**") and s.endswith("**")
    if bold:
        s = s.strip("*").strip()
    body = NUM_PREFIX.sub("", s, count=1).strip()
    if bold:
        return f"**{n}. {body}**"
    return f"{n}. {body}"


def main() -> None:
    text = AGENT_MD.read_text(encoding="utf-8")
    if not text.strip():
        raise SystemExit(f"{AGENT_MD} is empty — save the file in the editor first.")

    lines = text.splitlines()
    out: list[str] = []
    n = 0
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        n += 1
        out.append(renumber_line(line, n))

    trailing = "\n" if text.endswith("\n") else ""
    AGENT_MD.write_text("\n".join(out) + trailing, encoding="utf-8")
    print(f"Renumbered {n} questions in {AGENT_MD}")


if __name__ == "__main__":
    main()

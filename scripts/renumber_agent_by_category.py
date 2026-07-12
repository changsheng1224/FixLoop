"""Renumber questions within each category section (1..N per category)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_MD = ROOT / "docs" / "agent.md"

SECTION_RE = re.compile(r"^## (\d+)\.\s+(.+)$")
QUESTION_RE = re.compile(r"^(\*\*)?(\d+)\.\s*(.+?)(\*\*)?$")


def main() -> None:
    lines = AGENT_MD.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    i = 0
    categories: list[tuple[str, str, int]] = []  # (num, title, count)

    # Copy header until first category section
    while i < len(lines):
        line = lines[i]
        if SECTION_RE.match(line.strip()):
            break
        out.append(line)
        i += 1

    # Update header note if present
    for j, line in enumerate(out):
        if line.startswith("> 共"):
            out[j] = "> 共 **499** 题 · **10** 类 · **每类独立编号**（各类内从 1 开始）"
            break

    # Find and replace TOC table rows (between ## 目录 and ---)
    toc_start = None
    toc_end = None
    for j, line in enumerate(out):
        if line.strip() == "## 目录":
            toc_start = j
        if toc_start is not None and line.strip() == "---" and j > toc_start:
            toc_end = j
            break

    # Process each category section
    section_outputs: list[list[str]] = []
    while i < len(lines):
        m = SECTION_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue

        cat_num, cat_title = m.group(1), m.group(2)
        section: list[str] = [lines[i]]
        i += 1

        # Description line (>)
        if i < len(lines) and lines[i].startswith(">"):
            section.append(lines[i])
            i += 1

        # Blank after description
        if i < len(lines) and not lines[i].strip():
            section.append(lines[i])
            i += 1

        local_n = 0
        while i < len(lines):
            line = lines[i]
            if SECTION_RE.match(line.strip()) or (line.strip() == "---" and i + 1 < len(lines) and SECTION_RE.match(lines[i + 1].strip())):
                break
            if line.strip() == "---":
                i += 1
                break

            s = line.strip()
            if not s:
                section.append(line)
                i += 1
                continue

            qm = QUESTION_RE.match(s)
            if qm:
                local_n += 1
                body = qm.group(3).strip()
                if qm.group(1):
                    section.append(f"**{local_n}. {body}**")
                else:
                    section.append(f"{local_n}. {body}")
            else:
                section.append(line)
            i += 1

        categories.append((cat_num, cat_title, local_n))
        section_outputs.append(section)

    # Rebuild TOC
    if toc_start is not None and toc_end is not None:
        new_toc = out[: toc_start + 1]
        new_toc.extend(
            [
                "",
                "| # | 分类 | 题量 | 本类题号 |",
                "|---|------|------|----------|",
            ]
        )
        for cat_num, cat_title, count in categories:
            rng = f"1–{count}" if count else "—"
            new_toc.append(f"| {cat_num} | {cat_title} | {count} | {rng} |")
        new_toc.extend(out[toc_end:])
        out = new_toc

    # Append sections
    for idx, section in enumerate(section_outputs):
        if idx > 0:
            out.append("---")
            out.append("")
        out.extend(section)

    trailing = "\n" if lines and not lines[-1].strip() else ""
    if not out[-1].strip():
        pass
    else:
        trailing = "\n"
    AGENT_MD.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")

    total = sum(c for _, _, c in categories)
    print(f"Renumbered {len(categories)} categories, {total} questions -> {AGENT_MD}")
    for cat_num, cat_title, count in categories:
        print(f"  [{cat_num}] {cat_title}: 1–{count}")


if __name__ == "__main__":
    main()

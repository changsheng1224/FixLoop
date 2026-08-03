"""Heuristic text segmenter for multi-intent routing."""

from __future__ import annotations

import re

from agent_runtime.intent.models import Segment

# Protect dotted tokens like file.py / v1.2 from sentence splits.
_PROTECTED = re.compile(
    r"(?P<file>\b[\w./\\-]+\.(?:py|js|ts|java|go|rs|md|txt|yaml|yml|toml|json)\b)"
    r"|(?P<ver>\bv?\d+\.\d+(?:\.\d+)?\b)",
    re.IGNORECASE,
)

_SENT_END = re.compile(
    r"(?<=[。！？?!])\s*|"
    r"(?<=\.)(?=\s+(?:[A-Z\u4e00-\u9fff「\"']|(?:then|also|plus|besides)\b))",
    re.IGNORECASE,
)

# CJK cues must not use \b (no boundary between CJK letters).
_CUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*(然后|接著|接着|之后|之後)"), "sequential"),
    (re.compile(r"^\s*(and\s+then|then)\b", re.I), "sequential"),
    (re.compile(r"^\s*(另外|同时|同時|此外|并且|並且)"), "additive"),
    (re.compile(r"^\s*(also|plus|besides)\b", re.I), "additive"),
]

_FENCE = re.compile(r"```[\w+-]*\n.*?```", re.S)
_STACKISH = re.compile(r"(?i)traceback\s*\(most recent|^\s*File\s+\"", re.M)
_INTENT_LEAD = re.compile(r"(?i)记住|remember|/help|/cancel|请记住")


def _protect(text: str) -> tuple[str, list[str]]:
    tokens: list[str] = []

    def repl(m: re.Match[str]) -> str:
        tokens.append(m.group(0))
        return f"\x00P{len(tokens) - 1}\x00"

    return _PROTECTED.sub(repl, text), tokens


def _unprotect(text: str, tokens: list[str]) -> str:
    def repl(m: re.Match[str]) -> str:
        return tokens[int(m.group(1))]

    return re.sub(r"\x00P(\d+)\x00", repl, text)


def _split_sentences(block: str) -> list[str]:
    protected, tokens = _protect(block)
    parts = _SENT_END.split(protected)
    out: list[str] = []
    for p in parts:
        p = _unprotect(p.strip(), tokens)
        if p:
            out.append(p)
    return out if out else ([block.strip()] if block.strip() else [])


def _cue_for(text: str) -> tuple[str, str | None]:
    for pat, cue in _CUE_PATTERNS:
        m = pat.match(text)
        if m:
            rest = text[m.end() :].lstrip(" ，,")
            return (rest or text), cue
    return text, None


def _mask_fences(text: str) -> tuple[str, list[str]]:
    """Replace fenced code with placeholders so blank-line split won't cut them."""
    chunks: list[str] = []

    def repl(m: re.Match[str]) -> str:
        chunks.append(m.group(0))
        return f"\n\x00FENCE{len(chunks) - 1}\x00\n"

    return _FENCE.sub(repl, text), chunks


def _unmask_fences(text: str, chunks: list[str]) -> str:
    def repl(m: re.Match[str]) -> str:
        return chunks[int(m.group(1))]

    return re.sub(r"\x00FENCE(\d+)\x00", repl, text)


def _coalesce_into_stack(pieces: list[str]) -> list[str]:
    """Attach preceding prose/code paste onto the traceback piece."""
    if not pieces:
        return pieces
    out: list[str] = []
    i = 0
    while i < len(pieces):
        piece = pieces[i]
        if _STACKISH.search(piece):
            buf: list[str] = []
            while out and not _INTENT_LEAD.search(out[-1].split("\n", 1)[0]):
                # pull back code/prose; stop before hard intent leads
                prev = out[-1]
                if _STACKISH.search(prev):
                    break
                buf.append(out.pop())
            buf.reverse()
            merged = "\n\n".join(buf + [piece]) if buf else piece
            out.append(merged)
            i += 1
            continue
        out.append(piece)
        i += 1
    return out


def segment(text: str) -> list[Segment]:
    """Split user text into segments with optional sequential/additive cues."""
    if not text or not text.strip():
        return []

    masked, fences = _mask_fences(text.strip())

    # 1) blank-line blocks (fences already atomic placeholders)
    raw_blocks = re.split(r"\n\s*\n+", masked)
    pieces: list[str] = []
    for block in raw_blocks:
        block = _unmask_fences(block.strip(), fences)
        if not block:
            continue
        if _STACKISH.search(block) or block.strip().startswith("```"):
            pieces.append(block)
            continue
        lines = block.split("\n")
        buf: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            cued = False
            for pat, _ in _CUE_PATTERNS:
                if pat.match(stripped):
                    cued = True
                    break
            if cued and buf:
                pieces.append("\n".join(buf))
                buf = [stripped]
            else:
                buf.append(stripped)
        if buf:
            pieces.append("\n".join(buf))

    pieces = _coalesce_into_stack(pieces)

    # 2) sentence split each piece (keep stack / fences intact)
    sentences: list[str] = []
    for piece in pieces:
        if _STACKISH.search(piece) or piece.strip().startswith("```"):
            sentences.append(piece)
        else:
            sentences.extend(_split_sentences(piece.replace("\n", " ")))

    # 3) cue strip + short fragment merge (+ same-sentence multi-intent expand)
    from agent_runtime.intent.rules import split_same_sentence_multi

    segments: list[Segment] = []
    for raw in sentences:
        cleaned, cue = _cue_for(raw)
        if not cleaned.strip():
            continue
        parts = split_same_sentence_multi(cleaned) or [cleaned]
        for pi, part in enumerate(parts):
            part = part.strip()
            if not part:
                continue
            part_cue = cue if pi == 0 else ("sequential" if cue else "sequential")
            if pi > 0:
                part_cue = "sequential"
            if segments and len(part) < 2:
                prev = segments[-1]
                merged = f"{prev.text} {part}".strip()
                segments[-1] = Segment(index=prev.index, text=merged, cue=prev.cue)
                continue
            segments.append(
                Segment(index=len(segments), text=part, cue=part_cue if pi == 0 else "sequential")
            )

    # If any segment has a stack, fold preceding non-intent segments into it
    # (handles "帮我看看：" + code+stack still split as 2).
    if len(segments) > 1:
        stack_idxs = [i for i, s in enumerate(segments) if _STACKISH.search(s.text)]
        if stack_idxs:
            si = stack_idxs[0]
            lead = []
            keep_before: list[Segment] = []
            for j in range(si):
                head = segments[j].text.split("\n", 1)[0]
                if _INTENT_LEAD.search(head) and re.search(
                    r"(?i)记住|remember", head
                ):
                    keep_before.append(segments[j])
                else:
                    lead.append(segments[j].text)
            if lead:
                merged_text = "\n\n".join(lead + [segments[si].text])
                new_segs = keep_before + [
                    Segment(index=0, text=merged_text, cue=segments[si].cue)
                ]
                new_segs.extend(segments[si + 1 :])
                segments = new_segs

    for i, seg in enumerate(segments):
        seg.index = i
    return segments

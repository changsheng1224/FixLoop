"""Bounded reference projections; source selection never certifies technical correctness."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from agent_runtime.web_tools import validate_web_arguments


def canonical_web_url(url):
    validate_web_arguments("web_fetch", {"url": url})
    parsed = urlsplit(url)
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    port = parsed.port
    authority = host if port in {None, 80 if parsed.scheme == "http" else 443} else f"{host}:{port}"
    if ":" in host:
        authority = f"[{host}]" + (f":{port}" if port else "")
    path = parsed.path or "/"
    if host in {"github.com", "raw.githubusercontent.com"}:
        parts = path.strip("/").split("/")
        if host == "github.com" and len(parts) >= 5 and parts[2] == "blob":
            parts.pop(2)
        elif host != "raw.githubusercontent.com":
            parts = []
        if len(parts) >= 4:
            # Raw GitHub also spells a branch ref as refs/heads/<name>.
            # Keep explicit tag refs and distinct revisions separate.
            if parts[2:4] == ["refs", "heads"]:
                del parts[2:4]
            authority, path = "raw.githubusercontent.com", "/" + "/".join(parts)
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}
        ]
    )
    return urlunsplit((parsed.scheme, authority, path, query, ""))


def _focused_content(content, focus):
    """Select at most three disjoint source ranges without rewriting Markdown/code."""
    if not focus:
        return content, [{"start": 0, "end": len(content)}], "full"
    terms = list(dict.fromkeys(re.findall(r"\w{2,}", focus.casefold())))[:16]
    candidates = set()
    for term in terms:
        for number, match in enumerate(re.finditer(re.escape(term), content, re.IGNORECASE)):
            if number == 3:
                break
            index = match.start()
            start = max(0, content.rfind("\n", 0, max(0, index - 200)) + 1)
            start = max(start, index - 300)
            candidates.add((start, min(len(content), start + 1500)))
    ranked = sorted(
        candidates,
        key=lambda pair: (
            -sum(term in content[pair[0] : pair[1]].casefold() for term in terms),
            pair[0],
        ),
    )
    selected = []
    for start, end in ranked:
        if all(end <= a or start >= b for a, b in selected):
            selected.append((start, end))
        if len(selected) == 3:
            break
    if not selected:
        return content[:1500], [{"start": 0, "end": min(1500, len(content))}], "no_focus_match"
    selected.sort()
    ranges = [{"start": start, "end": end} for start, end in selected]
    return "\n\n[... source gap ...]\n\n".join(content[a:b] for a, b in selected), ranges, "focus"


def project_web_reference(name, data, args, config, notice):
    research = {
        "purpose": args.get("purpose", ""),
        "target_version": args.get("target_version", ""),
        "version_verification": "unverified",
        "domains": args.get("domains", []),
    }
    truncated = False
    if name == "web_search":
        rows, seen = [], set()
        dropped = {"invalid_url": 0, "outside_domains": 0, "duplicate": 0}
        shortened = 0
        for row in data["results"]:
            try:
                canonical = canonical_web_url(row["url"])
            except (ValueError, UnicodeError):
                dropped["invalid_url"] += 1
                continue
            host = urlsplit(row["url"]).hostname.encode("idna").decode("ascii").lower().rstrip(".")
            if research["domains"] and not any(
                host == domain or host.endswith("." + domain) for domain in research["domains"]
            ):
                dropped["outside_domains"] += 1
                continue
            if canonical in seen:
                dropped["duplicate"] += 1
                continue
            seen.add(canonical)
            summary = row["summary"] or row["snippet"] or row["content"]
            shortened += int(len(summary) > config.summary_chars)
            truncated |= (
                bool(row["content"])
                or len(summary) > config.summary_chars
                or len(row["title"]) > 240
            )
            rows.append(
                {
                    "title": row["title"][:240],
                    "url": row["url"],
                    "canonical_url": canonical,
                    "summary": summary[: config.summary_chars],
                    "summary_truncated": len(summary) > config.summary_chars,
                    **{k: row.get(k) for k in ("rerank_score", "authority", "date")},
                }
            )
        limited = max(0, len(rows) - args["count"])
        rows = rows[: args["count"]]
        truncated |= limited > 0
        research.update(
            provider_results=len(data["results"]),
            selected_results=len(rows),
            dropped=dropped,
            shortened_summaries=shortened,
            limited_results=limited,
        )
        projected = {"results": rows, "research": research}
        if not rows:
            research["guidance"] = (
                "No usable source; revise the question/scope or use local evidence."
            )
    else:
        text, ranges, selection = _focused_content(data["content"], args.get("focus", ""))
        research.update(
            source_url=args["url"],
            focus=args.get("focus", ""),
            selection=selection,
            source_ranges=ranges,
            source_chars=len(data["content"]),
            code_format_verification="unverified",
        )
        truncated = selection != "full" and text != data["content"]
        projected = {
            "title": data["title"],
            "url": args["url"],
            "content": text,
            "research": research,
        }
    warning = "Version applicability is unverified. Check the target dependency version."
    if name == "web_fetch":
        warning += " Code formatting/completeness is unverified; check the original source."
    rendered = notice + "\n" + warning + "\n" + json.dumps(projected, ensure_ascii=False)
    truncated |= len(rendered) > config.preview_chars
    rendered = rendered[: config.preview_chars]
    if truncated:
        rendered += (
            "\n[Preview shortened; use expand_observation with the OBS reference for source.]"
        )
    raw = {
        "trust": "untrusted_external",
        "notice": notice,
        "research": research,
        "provider_response": data,
    }
    return (
        rendered,
        projected,
        {"raw_result": raw, "source_dependencies": {}, "web_research": research},
        truncated,
    )

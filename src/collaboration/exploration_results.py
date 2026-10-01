"""Source validation and deterministic merging; findings remain owner candidates."""

from __future__ import annotations

from copy import deepcopy

from agent_runtime.plan_runtime.models import digest


def structured_result(raw: dict, task, observations: dict) -> dict:
    """Bind model claims to runtime observations, never model-provided versions."""
    if not isinstance(raw, dict) or set(raw) - {"summary", "findings", "unknowns"}:
        raise ValueError("invalid_exploration_result")
    summary, claims, unknowns = (
        raw.get("summary", ""),
        raw.get("findings", []),
        raw.get("unknowns", []),
    )
    if not isinstance(summary, str) or not isinstance(claims, list) or len(claims) > 32:
        raise ValueError("invalid_exploration_findings")
    if (
        not isinstance(unknowns, list)
        or len(unknowns) > 32
        or any(not isinstance(v, str) for v in unknowns)
    ):
        raise ValueError("invalid_exploration_unknowns")
    findings = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) - {
            "claim_key",
            "statement",
            "path",
            "range",
            "observation_id",
        }:
            raise ValueError("invalid_exploration_claim")
        key, statement = claim.get("claim_key"), claim.get("statement")
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError("invalid_exploration_claim_key")
        if not isinstance(statement, str) or not statement or len(statement) > 1000:
            raise ValueError("invalid_exploration_statement")
        observation = observations.get(claim.get("observation_id"))
        if observation is None:
            raise ValueError("claim_observation_not_owned")
        hit = next(
            (
                h
                for h in observation["hits"]
                if h["path"] == claim.get("path") and h.get("range") == claim.get("range")
            ),
            None,
        )
        if hit is None or not observation["file_versions"].get(hit["path"]):
            raise ValueError("claim_source_or_full_version_missing")
        findings.append(
            {
                "claim_key": key,
                "statement": statement,
                "category": task.kind,
                "path": hit["path"],
                "range": hit.get("range"),
                "file_hash": observation["file_versions"][hit["path"]],
                "scope": task.payload["exploration"]["scope_paths"],
                "resolution": "candidate" if observation["tool"] == "list_files" else "parsed",
                "sources": [
                    {
                        "observation_id": observation["observation_id"],
                        "checksum": observation["checksum"],
                        "tool": observation["tool"],
                        "task_id": task.task_id,
                        "attempt_id": observation["attempt_id"],
                    }
                ],
                "review": "candidate",
            }
        )
    if not findings:
        unknowns = [*unknowns, "No findings in the observed scope; repository absence is unproven."]
    if task.kind == "related_tests":
        unknowns = [*unknowns, "Tests were discovered only; execution and coverage are unverified."]
    return {
        "summary": summary[:1000],
        "findings": findings,
        "unknowns": [v[:500] for v in unknowns],
    }


def merge_findings(results: list[dict]) -> list[dict]:
    merged, by_exact, by_target = [], {}, {}
    for result in results:
        if result["status"] != "completed":
            continue
        for raw in result.get("findings", []):
            finding = deepcopy(raw)
            target = digest(
                {k: finding[k] for k in ("claim_key", "path", "range", "file_hash", "scope")}
            )
            exact = digest(
                {
                    **{k: v for k, v in finding.items() if k not in {"sources", "review"}},
                    "target": target,
                }
            )
            if exact in by_exact:
                existing = by_exact[exact]
                for source in finding["sources"]:
                    if source not in existing["sources"]:
                        existing["sources"].append(source)
                continue
            siblings = by_target.setdefault(target, [])
            if any(f["statement"] != finding["statement"] for f in siblings):
                for sibling in siblings:
                    sibling["review"] = "needs_review"
                finding["review"] = "needs_review"
            siblings.append(finding)
            by_exact[exact] = finding
            merged.append(finding)
    return merged

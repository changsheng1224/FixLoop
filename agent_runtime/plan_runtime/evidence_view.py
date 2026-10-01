"""Bounded record projections and final-request consumption, without inference."""

from __future__ import annotations

import json
import re
from copy import deepcopy


def evidence_summary(record: dict, check: dict) -> dict:
    omitted = []

    def preview(name, value, limit=512):
        if isinstance(value, list | dict):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if len(text) > limit:
                omitted.append(name)
                return text[:limit]
            return deepcopy(value)
        text = str(value or "")
        if len(text) > limit:
            omitted.append(name)
        return text[:limit]

    kind = record["kind"]
    versions = record.get("file_versions", {})
    summary = {
        "evidence_ref": record["evidence_id"],
        "kind": kind,
        "record_checksum_prefix": record["checksum"][:12],
        "status": check["status"],
        "use": check["use"],
        "whole_workspace": bool(record.get("whole_workspace")),
    }
    if not summary["whole_workspace"] and kind != "observation_present":
        summary["file_paths_preview"] = preview("file_paths", list(versions))
    if kind == "observation_present":
        observation = record.get("observation_record", {})
        retrieval = observation.get("retrieval_result", {})
        summary.update(
            observation_id=record["observation_id"],
            tool=observation.get("tool", ""),
            arguments_preview=preview("arguments", record.get("tool_arguments", {})),
            retrieval={
                key: preview(key, retrieval[key])
                for key in (
                    "execution",
                    "completeness",
                    "scanned_scope",
                    "truncation_reasons",
                )
                if key in retrieval
            },
        )
    elif kind == "analysis_recorded":
        summary.update(
            conclusion_preview=preview("conclusion", record["conclusion"]),
            input_refs=preview("input_refs", record["input_refs"]),
            supports="semantic correctness is not certified",
        )
    elif kind == "patch_applied":
        receipt = record["receipt"]
        summary.update(
            changed_paths=preview("changed_paths", record["changed_paths"]),
            receipt={key: receipt[key] for key in ("receipt_id", "call_id", "run_id", "status")},
            execution_stopped=record["execution_stopped"],
            supports="confirmed_patch_for_recorded_versions; not permission to replay",
        )
    elif kind == "tests_passed":
        receipt = record["receipt"]
        summary.update(
            command_preview=preview("command", receipt["command"]),
            receipt={
                key: receipt[key]
                for key in ("run_id", "attempt_id", "completed", "all_passed", "total_tests")
            },
            supports="recorded_test_result_for_recorded_versions; not exhaustive coverage",
        )
    # Full checksums remain in inspections/manifests and pin the version map.
    # This prefix is only a display label, never an integrity check.
    if omitted:
        summary["omitted_fields"] = omitted
    return summary


def consumption_manifest(context, metadata, sections, *, tail=(), tail_refs=None):
    """Only explicit retained bodies count; a checked ref alone is not a body."""
    required_refs = set(context.get("evidence_refs", []))
    required = {
        (check["evidence_ref"], check["use"])
        for check in context.get("evidence_checks", [])
        if check["evidence_ref"] in required_refs
    }
    prepared = bool(metadata.get("request_hash"))
    records = {}

    def visit(check):
        key = (check["evidence_ref"], check["use"])
        if key in records:
            return
        oid = check.get("observation_id", "")
        view = metadata.get("code_evidence", {}).get(oid, {})
        forms = []
        if prepared and oid and view.get("ok", False):
            if sections.get("source") and oid in metadata.get("_source_observation_refs", []):
                forms.append("source_snippet")
            if sections.get("feedback") and oid in metadata.get("feedback_observation_refs", []):
                forms.append("tool_feedback")
            for _, user in tail:
                if any(
                    (tail_refs or {}).get(block.get("tool_use_id")) == oid
                    and bool(block.get("content"))
                    and not str(block.get("content", "")).startswith("[source selected:")
                    for block in user["content"]
                    if block.get("type") == "tool_result"
                ):
                    forms.append("native_tool_result")
            body = view.get("body", "")
            segment = re.search(
                rf"(?ms)^\*\*tool\*\*: \[{re.escape(oid)}\](.*?)"
                r"(?=^\*\*(?:user|assistant|tool|system)\*\*:|\Z)",
                sections.get("history", ""),
            )
            if body and segment and body[: min(40, len(body))] in segment[1]:
                forms.append("history_excerpt")
        records[key] = {
            **{
                name: deepcopy(check[name])
                for name in (
                    "evidence_ref",
                    "kind",
                    "status",
                    "reason",
                    "use",
                    "record_checksum",
                    "observation_id",
                )
                if name in check
            },
            "required": key in required,
            "summary_in_request": prepared and bool(sections.get("state")) and key in required,
            "body_in_request": bool(forms),
            "body_forms": sorted(set(forms)),
            "body_reason": "selected"
            if forms
            else view.get("reason", "not_verified" if oid and not view else "not_selected"),
        }
        for dependency in check.get("dependencies", []):
            visit(dependency)

    for check in context.get("evidence_checks", []):
        visit(check)
    return list(records.values())

"""Controlled historical summaries; no claims about semantic model accuracy."""

from agent_runtime.history_summary import encode


def summary_json(fact="", **updates):
    value = {
        "confirmed_facts": [fact] if fact else [],
        "hypotheses": [],
        "rejected_hypotheses": [],
        "files_changed": [],
        "verification": {"status": "unknown", "details": []},
        "next_action": "",
        "evidence_refs": [],
    }
    value.update(updates)
    return encode(value)

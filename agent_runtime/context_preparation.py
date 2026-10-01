"""Pure encoding helpers; permissions and execution stay in the runtime."""

import hashlib
import json

from agent_runtime.errors import ContextBuildBlockedError


def request_payload(request, protocol):
    if protocol == "xml":
        return request.messages[0]["content"]
    return json.dumps(
        {
            "system": request.system_prompt,
            "messages": request.messages,
            "tools": request.tools,
            "tool_choice": (
                {"mode": request.tool_choice.mode.value, "name": request.tool_choice.name}
                if request.tool_choice
                else None
            ),
            "max_output_tokens": request.max_output_tokens,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def request_hash(request, protocol):
    return hashlib.sha256(request_payload(request, protocol).encode()).hexdigest()


def native_groups(messages):
    """Validate complete call/result groups before selecting any optional tail."""
    if len(messages) % 2:
        raise ContextBuildBlockedError("context_tool_pair_mismatch")
    groups = []
    seen = set()
    for index in range(0, len(messages), 2):
        assistant, user = messages[index : index + 2]
        if (
            not isinstance(assistant, dict)
            or not isinstance(user, dict)
            or assistant.get("role") != "assistant"
            or user.get("role") != "user"
        ):
            raise ContextBuildBlockedError("context_tool_pair_mismatch")
        if not isinstance(assistant.get("content"), list) or not isinstance(
            user.get("content"), list
        ):
            raise ContextBuildBlockedError("context_tool_pair_mismatch")
        if any(not isinstance(block, dict) for block in assistant["content"] + user["content"]):
            raise ContextBuildBlockedError("context_tool_pair_mismatch")
        calls = [
            block.get("id") for block in assistant["content"] if block.get("type") == "tool_use"
        ]
        results = [
            block.get("tool_use_id")
            for block in user["content"]
            if block.get("type") == "tool_result"
        ]
        if (
            not calls
            or not all(isinstance(call, str) and call for call in calls)
            or not all(isinstance(result, str) and result for result in results)
            or len(set(calls)) != len(calls)
            or len(set(results)) != len(results)
            or set(calls) != set(results)
            or seen.intersection(calls)
        ):
            raise ContextBuildBlockedError("context_tool_pair_mismatch")
        seen.update(calls)
        groups.append([assistant, user])
    return groups


def project_tail(groups, refs, metadata):
    """Preserve IDs and receipts; substitute checked source bodies/diagnostics."""
    selected = set(metadata.get("_source_observation_refs", []))
    projected = []
    for assistant, user in groups:
        blocks = []
        for block in user["content"]:
            oid = refs.get(str(block.get("tool_use_id", "")), "")
            evidence = metadata.get("code_evidence", {}).get(oid)
            if evidence and not evidence.get("ok"):
                block = {**block, "content": evidence["diagnostic"]}
            elif oid and oid in selected:
                block = {**block, "content": f"[source selected: {oid}]"}
            elif evidence:
                block = {**block, "content": evidence["content"]}
            blocks.append(block)
        projected.append([assistant, {**user, "content": blocks}])
    return projected


def pack_units(units, limit, budget, render, *, newest=False):
    """Keep whole units. A large candidate need not exclude smaller ones."""
    selected = []
    for unit in reversed(units) if newest else units:
        candidate = [unit, *selected] if newest else [*selected, unit]
        if budget.count(render(candidate)) <= limit:
            selected = candidate
    return selected

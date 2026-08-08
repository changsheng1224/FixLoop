from __future__ import annotations

from dataclasses import dataclass, field

from agent_runtime.features.memory.governance import (
    ConflictStatus,
    ConflictType,
    MemoryAuthority,
    MemoryGovernanceService,
    MemoryStatus,
)


@dataclass
class Candidate:
    key: str
    value: str
    source: str = "tool"
    source_type: str = "tool"
    confidence: float = 0.9
    evidence_refs: list[str] = field(default_factory=list)
    allow_multiple: bool = False


def _conflicting_service(**kwargs):
    service = MemoryGovernanceService({}, **kwargs)
    left = service.ingest(Candidate("test.command", "pytest"), scope="repository")
    right = service.ingest(Candidate("test.command", "tox"), scope="repository")
    service.consolidate()
    conflict = next(iter(service.conflicts.values()))
    return service, left, right, conflict


def test_conflict_record_is_stable_and_withheld_from_recall():
    service, left, right, conflict = _conflicting_service()

    assert conflict["conflict_type"] == ConflictType.HARD_CONTRADICTION.value
    assert conflict["status"] == ConflictStatus.PENDING.value
    assert set(conflict["candidate_ids"]) == {left.memory_id, right.memory_id}
    assert service.recall("test command") == []

    service.consolidate()
    assert len(service.conflicts) == 1

    service.resolve_pending_conflicts()
    service.resolve_pending_conflicts()
    assert service.stats["conflicts_unresolved"] == 1


def test_repository_policy_shadows_conflicting_memory():
    service = MemoryGovernanceService({})
    service.add_policy(
        "test.command",
        "pytest",
        authority=MemoryAuthority.REPO_POLICY.value,
        source="AGENTS.md",
    )
    pytest_memory = service.ingest(Candidate("test.command", "pytest"), scope="repository")
    service.ingest(Candidate("test.command", "tox"), scope="user")

    recalled = service.recall("test command")
    assert [item["memory_id"] for item in recalled] == [pytest_memory.memory_id]
    assert service.stats["policy_shadowed"] == 1


def test_current_repository_version_resolves_temporal_update(tmp_path):
    service = MemoryGovernanceService({}, repo_root=str(tmp_path))
    old = service.ingest(
        Candidate("api.shape", "v1"),
        scope="repository_version",
        repo_fingerprint="old",
    )
    current = service.ingest(
        Candidate("api.shape", "v2"),
        scope="repository_version",
        repo_fingerprint=service.current_repo_fingerprint,
    )

    service.consolidate()
    assert service.resolve_pending_conflicts() == 1
    assert service.registry[current.memory_id]["status"] == MemoryStatus.ACTIVE.value
    assert service.registry[old.memory_id]["status"] == MemoryStatus.STALE.value


def test_evidenced_higher_authority_source_resolves_model_disagreement():
    service = MemoryGovernanceService({})
    model = service.ingest(
        Candidate("test.command", "tox", source="", source_type="model"),
        scope="repository",
    )
    tool = service.ingest(
        Candidate(
            "test.command",
            "pytest",
            source="repo",
            source_type="tool",
            evidence_refs=["repo:pyproject.toml"],
        ),
        scope="repository",
    )
    service.bind_evidence(tool.memory_id, ["repo:pyproject.toml"])

    service.consolidate()
    assert service.resolve_pending_conflicts() == 1
    assert service.registry[tool.memory_id]["status"] == MemoryStatus.ACTIVE.value
    assert service.registry[model.memory_id]["status"] == MemoryStatus.REJECTED.value


def test_explicit_multi_value_keeps_both_candidates_active():
    service = MemoryGovernanceService({})
    first = service.ingest(
        Candidate("test.command", "pytest", allow_multiple=True), scope="repository"
    )
    second = service.ingest(
        Candidate("test.command", "tox", allow_multiple=True), scope="repository"
    )

    service.consolidate()
    assert service.resolve_pending_conflicts() == 1
    conflict = next(iter(service.conflicts.values()))
    assert conflict["conflict_type"] == ConflictType.MULTI_VALUE.value
    assert set(conflict["winner_ids"]) == {first.memory_id, second.memory_id}
    assert len(service.recall("test command")) == 2


def test_probe_runs_only_for_required_conflict_key():
    calls = []

    def probe(conflict, candidates):
        calls.append(conflict.key)
        winner = next(item for item in candidates if item["value"] == "pytest")
        return {
            "winner_ids": [winner["memory_id"]],
            "evidence_refs": ["verify:pytest-collect"],
            "reason": "collection passed",
        }

    service, left, _, conflict = _conflicting_service(probe=probe)
    assert service.resolve_pending_conflicts(required_keys={"build.command"}, allow_probe=True) == 0
    assert calls == []
    assert service.resolve_pending_conflicts(required_keys={"test.command"}, allow_probe=True) == 1
    assert calls == ["test.command"]
    assert service.conflicts[conflict["conflict_id"]]["resolver"] == "tool_probe"
    assert service.registry[left.memory_id]["status"] == MemoryStatus.ACTIVE.value


def test_resolve_for_query_and_management_lists():
    def probe(conflict, candidates):
        return {
            "winner_ids": [candidates[0]["memory_id"]],
            "evidence_refs": ["tool:probe"],
        }

    service, _, _, _ = _conflicting_service(probe=probe)
    assert len(service.list_memories(status=MemoryStatus.CONFLICTED.value)) == 2
    assert len(service.list_conflicts(status=ConflictStatus.PENDING.value)) == 1
    assert service.resolve_for_query("which test.command should run?") == 1
    assert len(service.list_conflicts(status=ConflictStatus.RESOLVED.value)) == 1


def test_external_memory_requires_local_cross_confirmation():
    external = Candidate(
        "build.command",
        "npm test",
        source="mcp:github",
        source_type="mcp",
    )
    service = MemoryGovernanceService({})
    memory = service.ingest(external, scope="repository")
    service.validate(memory.memory_id, passed=True)

    assert service.promote_eligible() == 0
    service.bind_evidence(memory.memory_id, ["repo:package.json"])
    assert service.promote_eligible() == 1
    assert service.registry[memory.memory_id]["cross_confirmed"] is True


def test_user_governance_confirm_pin_reject_forget_and_resolve():
    service, left, right, conflict = _conflicting_service()

    assert service.pin(left.memory_id)
    assert service.inspect(left.memory_id)["pinned"] is True
    assert service.user_resolve(conflict["conflict_id"], [left.memory_id])
    assert service.registry[left.memory_id]["authority"] == MemoryAuthority.USER_CONFIRMED.value
    assert service.registry[right.memory_id]["status"] == MemoryStatus.REJECTED.value
    assert service.reject(left.memory_id, reason="changed mind")
    assert service.forget(left.memory_id)
    assert service.inspect(left.memory_id) is None

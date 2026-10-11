"""Persisted workbench contracts, independent of Agent runtime sessions."""

from dataclasses import dataclass
from typing import Any, Literal

TurnMode = Literal["chat", "project_chat", "repair"]
NodeKind = Literal["turn", "local_checkpoint"]
RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "interrupted"]
TerminalStatus = Literal["succeeded", "failed", "cancelled", "interrupted"]


class WorkbenchError(Exception):
    """Base error for workbench callers."""


class NotFoundError(WorkbenchError):
    """An object does not exist in the requested conversation."""


class ConflictError(WorkbenchError):
    """Stale revision, busy conversation, or invalid execution transition."""


class SchemaError(WorkbenchError):
    """The database is not a supported workbench database."""


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    project_path: str | None
    base_snapshot_id: str | None
    active_branch_id: str
    view_node_id: str | None
    revision: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Branch:
    id: str
    conversation_id: str
    name: str
    fork_node_id: str | None
    head_node_id: str | None
    created_at: float


@dataclass(frozen=True)
class Node:
    id: str
    conversation_id: str
    parent_id: str | None
    kind: NodeKind
    user_text: str
    mode: TurnMode | None
    input_snapshot_id: str | None
    checkpoint_snapshot_id: str | None
    created_at: float


@dataclass(frozen=True)
class Run:
    id: str
    conversation_id: str
    node_id: str
    status: RunStatus
    answer: str
    error: str | None
    artifacts: dict[str, Any]
    output_snapshot_id: str | None
    created_at: float
    started_at: float | None
    finished_at: float | None


@dataclass(frozen=True)
class Snapshot:
    id: str
    conversation_id: str
    storage_key: str
    manifest: dict[str, Any]
    created_at: float


@dataclass(frozen=True)
class Turn:
    node: Node
    run: Run


@dataclass(frozen=True)
class ConversationTree:
    conversation: Conversation
    branches: tuple[Branch, ...]
    nodes: tuple[Node, ...]
    runs: tuple[Run, ...]
    snapshots: tuple[Snapshot, ...]

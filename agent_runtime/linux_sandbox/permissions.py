"""Trusted scope grants and exact-call approvals, outside the project filesystem.

Approval never changes the OS profile. Tickets are consumed by the native
backend, not sent to the project worker. Revisions fence concurrent revocation.
"""

from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path

from .receipts import ReceiptStore, _checksum, _read, _write
from .workspace_contract import WORKSPACE_READ_TOOLS, WORKSPACE_WRITE_TOOLS, relative_path

CAPABILITIES = frozenset(
    {
        "workspace_read",
        "workspace_write",
        "python",
        "pytest",
        "offline_install",
        "trusted_data",
        "trusted_control",
    }
)


class SandboxApprovalRequired(RuntimeError):  # noqa: N818 - public protocol name
    """Nonterminal pause for trusted orchestration, with no execution implied."""

    def __init__(self, approval_id):
        super().__init__("sandbox_approval_required")
        self.approval_id = approval_id


def _paths(values):
    if not isinstance(values, list | tuple) or len(values) > 256:
        raise ValueError("permission_invalid: paths")
    return tuple(sorted({relative_path(p, allow_root=True).casefold() for p in values}))


def _covers(scopes, path):
    path = relative_path(path, allow_root=True).casefold()
    return any(p == "." or p == path or path.startswith(p + "/") for p in scopes)


@dataclass(frozen=True)
class PermissionScope:
    read_paths: tuple[str, ...] = (".",)
    write_paths: tuple[str, ...] = (".",)
    capabilities: tuple[str, ...] = (
        "workspace_read",
        "workspace_write",
        "python",
        "pytest",
        "trusted_data",
        "trusted_control",
    )
    install_paths: tuple[str, ...] = ()

    def __post_init__(self):
        for key in ("read_paths", "write_paths", "install_paths"):
            object.__setattr__(self, key, _paths(getattr(self, key)))
        if not isinstance(self.capabilities, tuple | list) or any(
            c not in CAPABILITIES for c in self.capabilities
        ):
            raise ValueError("permission_invalid: unsupported capability")
        object.__setattr__(self, "capabilities", tuple(sorted(set(self.capabilities))))

    def allows(self, action):
        return (
            action["capability"] in self.capabilities
            and all(_covers(self.read_paths, p) for p in action["reads"])
            and all(_covers(self.write_paths, p) for p in action["writes"])
            and all(_covers(self.install_paths, p) for p in action["installs"])
        )

    def extend(self, action):
        return PermissionScope(
            self.read_paths + tuple(action["reads"]),
            self.write_paths + tuple(action["writes"]),
            self.capabilities + (action["capability"],),
            self.install_paths + tuple(action["installs"]),
        )


def request_action(request, input_versions=None):
    """Classify actual supported execution, including internal verifier calls."""
    from .models import SandboxRequest

    request = SandboxRequest.from_wire(request.to_wire())
    reads, writes, installs = [], [], []
    if request.operation == "workspace":
        name, args = request.tool_name, request.tool_args
        if name in WORKSPACE_WRITE_TOOLS:
            capability = "workspace_write"
            if name == "apply_patch":
                from agent_runtime.apply_patch_format import parse_apply_patch_text

                operations = parse_apply_patch_text(
                    args.get("patch") or args.get("diff") or args.get("input") or ""
                )
                writes = [relative_path(op.path) for op in operations]
            elif name == "workspace_restore":
                writes = list(args["files"])
            else:
                writes = [relative_path(args.get("path", ""))]
        elif name in WORKSPACE_READ_TOOLS:
            capability = "workspace_read"
            # Inventory/snapshot/environment describe the entire source view.
            reads = (
                [relative_path(args.get("path", "."), allow_root=True)]
                if name in {"read_file", "inspect_file", "list_files", "grep", "ast_parse"}
                else [relative_path(args["path"])]
                if name == "workspace_source_version"
                else ["."]
            )
        else:
            raise ValueError("policy_denied: tool not in workspace profile")
    else:
        from .command_profile import command_spec

        mode, parameters = command_spec(request)
        capability = {"pytest": "pytest", "install": "offline_install"}.get(mode, "python")
        # Arbitrary Python/pytest can read the entire OS-mounted source view.
        reads = ["."]
        if mode == "install":
            from .command_profile import install_inputs

            installs = list(install_inputs(request))
    wire = request.to_wire()
    wire.pop("call_id")  # Replay gets a fresh transport id, but identical authority/action.
    return {
        "request": wire,
        "capability": capability,
        "reads": list(_paths(reads)),
        "writes": list(_paths(writes)),
        "installs": list(_paths(installs)),
        "input_versions": dict(input_versions or {}),
    }


def approval_for_owner(action, coordinator, identity):
    """Rebind only the execution fence after a durable owner takeover.

    The saved proposal stays immutable. Arguments, task identity and input
    versions must still match before a fresh ticket can consume its approval.
    """
    if coordinator is None:
        return None
    from agent_runtime.run_coordination.store import CoordinationError

    try:
        coordinator.assert_can_dispatch()
    except CoordinationError:
        return None
    lease = coordinator.lease
    original = action.get("request", {})
    owner_key = "owner_token" if "operation" in original else "owner"
    if "operation" in original:
        if any(
            original.get(key) != getattr(lease, key)
            for key in ("workspace_id", "task_id", "run_id")
        ):
            return None
    elif (
        original.get("identity") != identity
        or identity.get("task_id", identity.get("task")) != lease.task_id
        or identity.get("run_id", identity.get("run")) != lease.run_id
    ):
        return None
    generation = original.get("generation", 0)
    if (
        not original.get(owner_key)
        or not 0 < generation <= lease.generation
        or (generation == lease.generation and original[owner_key] != lease.owner_token)
        or original.get("coordination_revision", 0) > lease.coordination_revision
    ):
        return None
    rebound = deepcopy(action)
    rebound["request"].update(
        {
            owner_key: lease.owner_token,
            "generation": lease.generation,
            "coordination_revision": lease.coordination_revision,
        }
    )
    return rebound


class PermissionService:
    """Persistent authority. Model-facing tools cannot resolve or replace grants."""

    def __init__(self, identity, scope=None, *, state_root=None, workspace=None):
        self.identity = deepcopy(identity)
        self._mutex = threading.RLock()
        self._store = None
        self._path = None
        self._state_root = state_root
        if state_root is not None:
            from agent_runtime.state_root import trusted_state_root

            external = trusted_state_root(workspace, state_root)
            self._store = ReceiptStore(external / "permissions", Path(workspace))
            self._path = self._store.root / (_checksum(self.identity) + ".permissions.json")
        self._memory = {
            "schema": 1,
            "identity": self.identity,
            "revision": 1,
            "scope": asdict(scope or PermissionScope()),
            "pending": None,
            "tickets": {},
            "audit": [],
            "used_calls": [],
        }
        with self._locked() as state:
            self._save(state)

    @classmethod
    def for_binding(cls, binding, scope=None):
        return cls(
            binding.identity,
            scope,
            state_root=binding.state_root,
            workspace=binding.mapping.host_root,
        )

    @contextmanager
    def _locked(self):
        with self._mutex:
            if self._store is None:
                yield deepcopy(self._memory)
            else:
                with self._store.lock():
                    state = _read(self._path) or deepcopy(self._memory)
                    if (
                        state.get("schema") != 1
                        or state.get("identity") != self.identity
                        or type(state.get("revision")) is not int
                        or state["revision"] < 1
                    ):
                        raise ValueError("permission_state_invalid")
                    self._validate_state(state)
                    yield state

    def _validate_state(self, state):
        try:
            PermissionScope(**state["scope"])
            if (
                not isinstance(state["tickets"], dict)
                or len(state["tickets"]) > 256
                or not isinstance(state["audit"], list)
                or not isinstance(state["used_calls"], list)
            ):
                raise ValueError("state types")
            pending = state["pending"]
            if pending is not None and (
                not isinstance(pending, dict)
                or pending["status"] not in {"pending", "approved", "denied", "stale", "consumed"}
                or pending["action_digest"] != _checksum(pending["action"])
                or type(pending["revision"]) is not int
                or not pending["id"]
            ):
                raise ValueError("pending integrity")
            for key, ticket in state["tickets"].items():
                if (
                    ticket["call_id"] != key
                    or type(ticket["revision"]) is not int
                    or not ticket["authorization_id"]
                    or not ticket["action_digest"]
                ):
                    raise ValueError("ticket integrity")
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("permission_state_invalid") from exc

    def _save(self, state):
        if self._path is not None:
            _write(self._path, state)
        self._memory = deepcopy(state)

    def _policy(self, state):
        return _checksum(
            {"identity": self.identity, "revision": state["revision"], "scope": state["scope"]}
        )

    def _audit(self, state, event, **data):
        entry = {"event": event, "revision": state["revision"], **data}
        state["audit"] = [*state["audit"], entry][-512:]

    def snapshot(self):
        with self._locked() as state:
            return deepcopy({**state, "policy_digest": self._policy(state)})

    def checkpoint(self):
        state = self.snapshot()
        return {
            "identity": state["identity"],
            "revision": state["revision"],
            "pending": state["pending"],
            "policy_digest": state["policy_digest"],
        }

    def resolve(self, approval_id, *, allow):
        """Trusted UI/API decision; never execute as a side effect of approval."""
        if type(allow) is not bool:
            raise ValueError("permission_invalid: explicit decision required")
        with self._locked() as state:
            self._assert_live()
            pending = state["pending"]
            if not pending or pending["id"] != approval_id or pending["status"] != "pending":
                raise ValueError("approval_stale")
            state["revision"] += 1
            pending["status"] = "approved" if allow else "denied"
            pending["revision"] = state["revision"]
            state["tickets"] = {}
            self._audit(
                state,
                pending["status"],
                approval_id=approval_id,
                action_digest=pending["action_digest"],
            )
            self._save(state)

    def replace_scope(self, scope):
        """Trusted revocation/explicit preauthorization, invalidating old tickets."""
        if not isinstance(scope, PermissionScope):
            raise ValueError("permission_invalid: scope")
        with self._locked() as state:
            state["scope"] = asdict(scope)
            state["revision"] += 1
            state["tickets"] = {}
            if state["pending"]:
                state["pending"]["status"] = "stale"
            self._audit(state, "scope_changed")
            self._save(state)

    def initialize_scope(self, scope):
        """Apply the trusted task's initial scope once, before its first decision."""
        with self._locked() as state:
            if state["revision"] == 1 and not state["audit"] and not state["tickets"]:
                state["scope"] = asdict(scope)
                self._save(state)

    def authorize(
        self,
        request,
        *,
        callback=None,
        policy="ask",
        cancel_token=None,
        restore_owned=None,
        input_versions=None,
        coordinator=None,
    ):
        for path, expected in request.base_versions.items():
            if input_versions is not None and input_versions.get(path) != expected:
                raise ValueError("stale_preimage: input changed before permission decision")
        if request.tool_name == "workspace_restore":
            files = request.tool_args.get("files")
            if (
                not isinstance(files, dict)
                or not files
                or restore_owned is None
                or set(request.base_versions) != set(files)
                or any(
                    p not in restore_owned or restore_owned[p] != request.base_versions[p]
                    for p in files
                )
            ):
                raise ValueError("stale_precondition: restore requires runtime-owned preimages")
        return self.authorize_action(
            request_action(request, input_versions),
            request.call_id,
            callback=callback,
            policy=policy,
            cancel_token=cancel_token,
            coordinator=coordinator,
        )

    def authorize_action(
        self, action, call_id, *, callback=None, policy="ask", cancel_token=None, coordinator=None
    ):
        if coordinator is not None:
            coordinator.assert_can_dispatch()
        # JSON detachment prevents an approval callback mutating the proposed invocation.
        action = json.loads(json.dumps(action, sort_keys=True))
        digest = _checksum(action)
        if action["capability"] not in CAPABILITIES:
            raise ValueError("policy_denied: unsupported capability")
        if cancel_token is not None and cancel_token.is_cancelled:
            return {"status": "cancelled", "action_digest": digest}
        with self._locked() as state:
            self._assert_live()
            if call_id in state["used_calls"] or call_id in state["tickets"]:
                raise ValueError("authorization_replayed")
            pending = state["pending"]
            matches_pending = bool(pending) and (
                pending["action_digest"] == digest
                or approval_for_owner(pending["action"], coordinator, self.identity) == action
            )
            scope = PermissionScope(**state["scope"])
            grant = False
            internal_read = action["capability"] == "workspace_read" and scope.allows(action)
            if pending and pending["status"] in {"pending", "approved"} and not internal_read:
                if not matches_pending:
                    return {"status": "approval_pending", "approval_id": pending["id"]}
                if pending["status"] == "approved":
                    if pending["revision"] != state["revision"]:
                        raise ValueError("approval_stale")
                    scope = scope.extend(action)
                    grant = True
            if pending and matches_pending and pending["status"] in {"denied", "stale"}:
                return {
                    "status": (
                        "approval_denied" if pending["status"] == "denied" else "approval_stale"
                    ),
                    "approval_id": pending["id"],
                }
            if scope.allows(action):
                evidence = {
                    "status": "allowed",
                    "authorization_id": "auth-" + uuid.uuid4().hex,
                    "action_digest": digest,
                    "revision": state["revision"],
                    "policy_digest": self._policy(state),
                    "call_id": call_id,
                }
                if grant:
                    evidence["grant_scope"] = asdict(scope)
                    evidence["approval_digest"] = pending["action_digest"]
                if call_id in state["tickets"]:
                    raise ValueError("authorization_replayed")
                # Only outstanding tickets are retained; the audit retains consumed identities.
                if len(state["tickets"]) >= 256:
                    raise ValueError("permission_ticket_limit")
                state["tickets"][call_id] = evidence
                self._audit(
                    state,
                    "allowed",
                    **{k: v for k, v in evidence.items() if k != "status"},
                )
                self._save(state)
                return evidence
            if not matches_pending or pending["status"] == "consumed":
                pending = {
                    "id": "approval-" + uuid.uuid4().hex,
                    "status": "pending",
                    "action": action,
                    "action_digest": digest,
                    "revision": state["revision"],
                }
                state["pending"] = pending
                self._audit(state, "pending", approval_id=pending["id"], action_digest=digest)
                self._save(state)
            proposal = deepcopy(pending)
        # No lock during a human interaction. 'auto' only permits existing scope.
        if policy == "never":
            self.resolve(proposal["id"], allow=False)
        elif callback is not None:
            try:
                decision = callback(deepcopy(proposal))
            except EOFError:
                decision = None
            except KeyboardInterrupt:
                if cancel_token is not None:
                    cancel_token.cancel("approval_interrupted")
                self.cancel_pending(proposal["id"])
                return {"status": "cancelled", "approval_id": proposal["id"]}
            if decision is True or decision is False:
                self.resolve(proposal["id"], allow=decision)
        state = self.snapshot()
        pending = state["pending"]
        if cancel_token is not None and cancel_token.is_cancelled:
            self.cancel_pending(proposal["id"])
            return {"status": "cancelled", "approval_id": proposal["id"]}
        if pending and pending["id"] == proposal["id"] and pending["status"] == "approved":
            return self.authorize_action(
                action, call_id, policy=policy, cancel_token=cancel_token, coordinator=coordinator
            )
        return {
            "status": "approval_denied"
            if pending and pending["status"] == "denied"
            else "approval_required",
            "approval_id": proposal["id"],
            "action_digest": digest,
            "revision": state["revision"],
            "policy_digest": self._policy(state),
            "proposal": proposal,
        }

    def _assert_live(self):
        if self._state_root is not None:
            from .lifecycle import assert_identity_live

            assert_identity_live(self._state_root, self.identity)

    def cancel_pending(self, approval_id=None):
        with self._locked() as state:
            pending = state["pending"]
            if (
                pending
                and pending["status"] in {"pending", "approved"}
                and (approval_id is None or pending["id"] == approval_id)
            ):
                state["revision"] += 1
                pending["status"] = "stale"
                state["tickets"] = {}
                self._audit(state, "cancelled", approval_id=pending["id"])
                self._save(state)

    def consume(self, request, *, input_versions=None):
        return self.consume_action(request_action(request, input_versions), request.call_id)

    def consume_action(self, action, call_id):
        digest = _checksum(action)
        with self._locked() as state:
            ticket = state["tickets"].get(call_id)
            if (
                not ticket
                or ticket["action_digest"] != digest
                or ticket["revision"] != state["revision"]
                or ticket["policy_digest"] != self._policy(state)
            ):
                raise ValueError("authorization_required: missing, changed or revoked ticket")
            del state["tickets"][call_id]
            # Fail closed at the limit instead of forgetting a consumed transport id.
            if len(state["used_calls"]) >= 16384:
                raise ValueError("permission_call_limit")
            state["used_calls"].append(call_id)
            if "grant_scope" in ticket:
                pending = state["pending"]
                if (
                    not pending
                    or pending["status"] != "approved"
                    or pending["action_digest"] != ticket["approval_digest"]
                ):
                    raise ValueError("approval_stale")
                state["scope"] = ticket.pop("grant_scope")
                pending["status"] = "consumed"
                ticket["policy_digest"] = self._policy(state)
                self._audit(
                    state,
                    "grant_committed",
                    approval_id=pending["id"],
                    action_digest=digest,
                )
            self._audit(state, "started", **{k: v for k, v in ticket.items() if k != "status"})
            self._save(state)
            return deepcopy(ticket)

    def assert_current(self, evidence):
        with self._locked() as state:
            if evidence["revision"] != state["revision"] or evidence[
                "policy_digest"
            ] != self._policy(state):
                raise ValueError("authorization_revoked")

    def discard(self, call_id):
        with self._locked() as state:
            if state["tickets"].pop(call_id, None) is not None:
                self._audit(state, "not_started", call_id=call_id)
                self._save(state)

    def outcome(self, evidence, status, receipt_id=""):
        with self._locked() as state:
            self._audit(
                state,
                "outcome",
                authorization_id=evidence["authorization_id"],
                action_digest=evidence["action_digest"],
                status=status,
                receipt_id=receipt_id,
            )
            self._save(state)


def permission_service(context):
    service = getattr(context, "sandbox_permissions", None)
    backend = context.sandbox_backend
    if backend is None:
        raise ValueError("sandbox_unavailable: permissions require a backend")
    authority = getattr(backend, "permissions", None)
    binding = getattr(context, "sandbox_task_binding", None)
    if service is not None:
        if (
            authority is not None
            and service is not authority
            or binding is not None
            and service.identity != binding.identity
        ):
            raise ValueError("sandbox_permission_binding_mismatch")
        if authority is None and binding is None:
            expected = {
                "workspace": context.sandbox_workspace_id,
                "task": context.sandbox_task_id,
                "run": context.sandbox_run_id,
            }
            if service.identity != expected:
                raise ValueError("sandbox_permission_binding_mismatch")
        return service
    # Batch context copies share this lock and the production backend authority.
    with context.sandbox_versions_lock:
        service = getattr(backend, "permissions", None)
        lock = getattr(context, "edit_lock", None)
        paths = tuple(sorted(lock.allowed_edit)) if lock is not None else (".",)
        scope = getattr(context, "sandbox_initial_scope", None) or PermissionScope(
            write_paths=paths
        )
        if service is None:
            binding = getattr(context, "sandbox_task_binding", None)
            service = (
                PermissionService.for_binding(binding, scope)
                if binding is not None
                else PermissionService(
                    {
                        "workspace": context.sandbox_workspace_id,
                        "task": context.sandbox_task_id,
                        "run": context.sandbox_run_id,
                    },
                    scope,
                )
            )
            backend.permissions = service
        else:
            service.initialize_scope(scope)
        binding = getattr(context, "sandbox_task_binding", None)
        if binding is not None and service.identity != binding.identity:
            raise ValueError("sandbox_permission_binding_mismatch")
        context.sandbox_permissions = service
        return service


def permission_rejection(decision):
    from agent_runtime.tool_result import ToolResult

    return ToolResult(
        content="Permission: " + decision["status"],
        status="rejected",
        error_code=decision["status"],
        retryable=False,
        metadata={
            "sandbox_permission": deepcopy(decision),
            "approval_id": decision.get("approval_id", ""),
            "termination_guaranteed": True,
            "sandbox_mutation_status": "not_started",
        },
    )


def authorize_trusted(context, name, args):
    """Only canonical pure-data/control implementations may call this entry."""
    from .tool_policy import SandboxToolAccess, sandbox_tool_access

    access = sandbox_tool_access(name)
    if access not in {SandboxToolAccess.TRUSTED_DATA, SandboxToolAccess.TRUSTED_CONTROL}:
        raise ValueError("policy_denied: trusted tool not audited")
    service = permission_service(context)
    coordinator = getattr(context, "run_coordinator", None)
    lease = coordinator.lease if coordinator is not None else None
    action = {
        "request": {
            "identity": service.identity,
            "tool": name,
            "args": args,
            "owner": getattr(context, "sandbox_owner_token", ""),
            "generation": getattr(context, "sandbox_generation", 0),
            "coordination_revision": getattr(context, "sandbox_coordination_revision", 0),
        },
        "capability": str(access),
        "reads": [],
        "installs": [],
        "writes": [relative_path(args.get("path", ""))] if name == "expand_lock" else [],
    }
    if lease is not None:
        action["request"].update(
            owner=lease.owner_token,
            generation=lease.generation,
            coordination_revision=lease.coordination_revision,
        )
    call_id = "trusted-" + uuid.uuid4().hex
    decision = service.authorize_action(
        action,
        call_id,
        callback=getattr(context, "sandbox_approval_callback", None),
        policy=getattr(context, "sandbox_approval_policy", "ask"),
        cancel_token=getattr(context, "cancel_token", None),
        coordinator=coordinator,
    )
    if decision["status"] != "allowed":
        return permission_rejection(decision), None
    evidence = service.consume_action(action, call_id)
    return None, evidence

"""Startup resolution of verification configuration into an immutable contract."""

from dataclasses import dataclass


@dataclass(frozen=True)
class VerificationBackend:
    tier: str = "container"
    languages: tuple[str, ...] = ("python",)


@dataclass(frozen=True)
class ExecutionPolicy:
    requested_tier: str
    enabled: bool
    host_fallback: bool
    static_fallback: bool
    container_available: bool

    def initial_tier(self, language: str) -> str:
        if self.requested_tier != "auto":
            return self.requested_tier
        if self.container_available and language == "python":
            return "container"
        if self.host_fallback:
            return "host"
        if self.static_fallback:
            return "static"
        return "container"


def resolve_execution_policy(
    *,
    sandbox_policy,
    execution_tier="",
    require_sandbox=False,
    sandbox=False,
    verification_backend=None,
    host=False,
    static=False,
):
    tier = execution_tier or (
        "static"
        if static and not host and verification_backend is None
        else "auto"
        if verification_backend is not None
        else "host"
    )
    if tier not in {"auto", "host", "container", "static"}:
        raise ValueError(f"unknown execution tier: {tier}")
    if sandbox_policy not in {"disabled", "preferred", "required"}:
        raise ValueError(f"unknown sandbox policy: {sandbox_policy}")
    required = require_sandbox or sandbox_policy == "required"
    if sandbox_policy == "disabled" and (required or sandbox or tier == "container"):
        raise ValueError("sandbox_policy=disabled conflicts with sandbox execution")
    if required and execution_tier in {"host", "static"}:
        raise ValueError("required sandbox conflicts with host/static verification")
    if sandbox:
        tier = "linux_sandbox"
    elif required:
        tier = "container"
    elif sandbox_policy == "disabled" and tier == "auto":
        tier = "static" if static and not host else "host"
    return ExecutionPolicy(
        tier,
        bool(sandbox or required or verification_backend is not None or host or static),
        bool(host and not required),
        bool(static and not required),
        bool(verification_backend is not None and sandbox_policy != "disabled"),
    )

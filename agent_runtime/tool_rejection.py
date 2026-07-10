"""工具拒绝双层语义：Gateway / Executor metadata 与 trace 字段。"""

from __future__ import annotations

__all__ = [
    "REJECTION_LAYER_EXECUTOR",
    "REJECTION_LAYER_GATEWAY",
    "TOOL_TRACE_PUBLIC_KEYS",
    "build_executor_error_metadata",
    "build_executor_rejection_metadata",
    "build_gate7_pass_metadata",
    "build_gateway_rejection_metadata",
    "build_rejection_observability_payload",
    "tool_trace_payload",
]

REJECTION_LAYER_GATEWAY = "gateway"
REJECTION_LAYER_EXECUTOR = "executor"

TOOL_TRACE_PUBLIC_KEYS = (
    "tool_status",
    "tool_error_code",
    "rejection_layer",
    "rejection_reason",
    "gate_id",
    "approval_policy",
    "approval_result",
)


def build_gateway_rejection_metadata(**extra) -> dict:
    """Layer 1 Gateway 拒绝 metadata。"""
    meta = {
        "tool_status": "rejected",
        "tool_error_code": "permission_denied",
        "rejection_layer": REJECTION_LAYER_GATEWAY,
        "rejection_reason": "role_not_allowed",
    }
    meta.update(extra)
    return meta


def build_rejection_observability_payload(summary: dict | None = None, **fields) -> dict:
    """Trace/report 观测字段：map 计数 + Grafana 友好的扁平行。

    ``tool_rejection_metrics`` 每行 ``{layer, gate_id, count}``，便于 Loki/JSON
    管道按 label 展开为时序。
    """
    data = dict(summary or {})
    for key, value in fields.items():
        if value:
            data[key] = value

    layer = data.get("tool_rejections_by_layer") or {}
    gate = data.get("tool_rejections_by_gate") or {}
    denied = data.get("permission_denied_by_tool") or {}
    if not layer and not gate and not denied:
        return {}

    payload: dict = {}
    if layer:
        payload["tool_rejections_by_layer"] = dict(layer)
    if gate:
        payload["tool_rejections_by_gate"] = dict(gate)
    if denied:
        payload["permission_denied_by_tool"] = dict(denied)

    metrics = []
    for gate_id, count in sorted(gate.items()):
        layer_name = REJECTION_LAYER_GATEWAY if gate_id == "gateway" else REJECTION_LAYER_EXECUTOR
        metrics.append(
            {
                "layer": layer_name,
                "gate_id": gate_id,
                "count": int(count or 0),
            }
        )
    if metrics:
        payload["tool_rejection_metrics"] = metrics

    gateway_total = int(layer.get("gateway", 0) or 0)
    if gateway_total:
        payload["gateway_denials"] = gateway_total

    return payload


def build_executor_rejection_metadata(gate_id: int, tool_error_code: str, **extra) -> dict:
    """Layer 2 Executor 闸口拒绝 metadata。"""
    meta = {
        "tool_status": "rejected",
        "tool_error_code": tool_error_code,
        "rejection_layer": REJECTION_LAYER_EXECUTOR,
        "gate_id": gate_id,
    }
    meta.update(extra)
    return meta


def build_executor_error_metadata(tool_error_code: str = "runtime_error", **extra) -> dict:
    """Layer 2 Gate 9 执行异常 metadata。"""
    meta = {
        "tool_status": "error",
        "tool_error_code": tool_error_code,
        "rejection_layer": REJECTION_LAYER_EXECUTOR,
        "gate_id": 9,
    }
    meta.update(extra)
    return meta


def build_gate7_pass_metadata(approval_policy: str) -> dict:
    """Gate 7 审批通过 metadata（高风险工具成功路径）。"""
    return {
        "gate_id": 7,
        "approval_policy": approval_policy,
        "approval_result": "auto_allowed" if approval_policy == "auto" else "user_approved",
    }


def tool_trace_payload(tool_name: str, metadata: dict | None) -> dict:
    """从 ToolExecutionResult.metadata 提取 trace 安全字段。"""
    meta = metadata or {}
    payload = {"tool": tool_name}
    for key in TOOL_TRACE_PUBLIC_KEYS:
        if key in meta:
            payload[key] = meta[key]
    return payload

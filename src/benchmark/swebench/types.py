"""SWE-bench Adapter 数据结构。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class FailureClass(StrEnum):
    """三类失败归因（计划验收）。"""

    NONE = "none"  # resolved / 成功路径
    ENV = "env"  # Docker / 数据集 / checkout / harness 安装
    BASELINE_DIRTY = "baseline_dirty"  # 运行前基线污染 / 非空 diff
    AGENT = "agent"  # 无 patch、超时、repair 异常
    EVAL = "eval"  # harness 跑通但未 resolved / patch 不可应用


@dataclass
class SweInstance:
    """Lite / Verified 单条实例（字段对齐官方 HF schema 子集）。"""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: str = ""  # gold patch（可选）
    test_patch: str = ""
    version: str = ""
    FAIL_TO_PASS: list[str] = field(default_factory=list)
    PASS_TO_PASS: list[str] = field(default_factory=list)
    environment_setup_commit: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> SweInstance:
        f2p = data.get("FAIL_TO_PASS") or data.get("fail_to_pass") or []
        p2p = data.get("PASS_TO_PASS") or data.get("pass_to_pass") or []
        if isinstance(f2p, str):
            import json

            try:
                f2p = json.loads(f2p)
            except json.JSONDecodeError:
                f2p = [f2p] if f2p else []
        if isinstance(p2p, str):
            import json

            try:
                p2p = json.loads(p2p)
            except json.JSONDecodeError:
                p2p = [p2p] if p2p else []
        return cls(
            instance_id=str(data.get("instance_id") or ""),
            repo=str(data.get("repo") or ""),
            base_commit=str(data.get("base_commit") or ""),
            problem_statement=str(data.get("problem_statement") or ""),
            patch=str(data.get("patch") or ""),
            test_patch=str(data.get("test_patch") or ""),
            version=str(data.get("version") or ""),
            FAIL_TO_PASS=list(f2p),
            PASS_TO_PASS=list(p2p),
            environment_setup_commit=str(data.get("environment_setup_commit") or ""),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InstanceResult:
    instance_id: str
    model_name_or_path: str = "fixloop"
    model_patch: str = ""
    resolved: bool | None = None
    failure_class: FailureClass = FailureClass.NONE
    failure_detail: str = ""
    duration_ms: int = 0
    repair_status: str = ""
    repair_run_id: str = ""
    repo_path: str = ""
    trace_hint: str = ""
    harness_log: str = ""
    error: str = ""
    verified: bool = False
    baseline_preflight: dict = field(default_factory=dict)

    def to_prediction(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "model_name_or_path": self.model_name_or_path,
            "model_patch": self.model_patch or "",
            "verified": bool(self.verified),
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d["failure_class"] = str(self.failure_class)
        return d

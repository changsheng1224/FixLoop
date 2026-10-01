"""Durable CLI progress and result artifacts, including failed preparation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from src.state import RepairState


class RepairReport:
    def __init__(self, output: Path, source: str):
        self.output = output
        self.data: dict = {
            "schema_version": "1.0",
            "source": source,
            "status": "preparing",
            "category": "",
            "error": "",
            "repo_path": "",
            "base_commit": "",
            "requested_ref": "",
            "baseline_tree": "",
            "runtime_run_id": "",
            "verification": None,
            "verification_tier": "",
            "changed_files": [],
            "patch_available": False,
            "events": [],
        }
        self.save()

    def progress(self, phase: str, message: str) -> None:
        self.data["events"].append(
            {
                "time": datetime.now(UTC).isoformat(),
                "phase": phase,
                "message": message,
            }
        )
        self.save()

    def record_state(self, state: RepairState, tier: str) -> None:
        from src.repair.verification.verify_diagnose import diagnose_verification

        vr = state.verification_result
        internal = (state.node_timings.get("phases_internal") or {}).get("verify") or {}
        actual_tier = internal.get("actual_tier", tier)
        tested = bool(
            vr
            and vr.all_passed
            and vr.total_tests > 0
            and actual_tier not in {"static", "skipped", "none"}
            and not self.data.get("dry_run")
        )
        status = str(state.status)
        if status in {"fixed", "patched"} and not tested:
            status = "pending_verify"
        self.data.update(
            status=status,
            runtime_status=str(state.status),
            runtime_run_id=state.repair_run_id,
            recovery=state.recovery_outcome,
            verification=vr.to_dict() if vr else None,
            verification_tier=actual_tier,
            verification_requested_tier=tier,
            verification_details=internal,
            verification_receipt=state.node_timings.get("plan_verification_receipt", {}),
            failure_tags=list(state.failure_tags),
            agent_errors=dict(state.agent_errors),
            verification_scope="runtime_selected_tests" if tested else "unverified",
        )
        if vr and not vr.all_passed:
            diagnosis = diagnose_verification(vr)
            self.data["category"] = (
                "verification_environment_failed" if diagnosis.is_env else "verification_failed"
            )
            self.data["error"] = "\n".join(vr.failure_logs) or diagnosis.guidance
        elif status not in {"fixed", "pending_verify"}:
            self.data["category"] = status
            self.data["error"] = "\n".join(str(error) for error in state.agent_errors.values())
        self.save()

    def fail(self, category: str, message: str) -> None:
        self.data.update(status="failed", category=category, error=message)
        if category in {"user_cancel", "timeout"}:
            self.data["status"] = category
        self.save()

    def save(self) -> None:
        target = self.output / "result.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        lines = [
            "# FixLoop 修复报告",
            "",
            f"状态：{self.data['status']}",
            f"仓库：{self.data['source']}",
            f"工作目录：{self.data['repo_path']}",
            f"基线 commit：{self.data['base_commit'] or '无 Git 基线'}",
            f"验证执行层：{self.data['verification_tier'] or '未执行'}",
            "",
        ]
        if self.data.get("verification_scope") == "runtime_selected_tests":
            vr = self.data["verification"]
            lines.append(f"验证：{vr['passed']}/{vr['total_tests']} 通过（运行时选择的测试范围）。")
        else:
            lines.append("尚无测试通过证据；静态检查和跳过验证不算修复已验证。")
        if self.data["error"]:
            lines.extend(["", f"失败类型：{self.data['category']}", self.data["error"]])
        if self.data["patch_available"]:
            lines.extend(["", "补丁：patch.diff", "", "修改文件："])
            lines.extend(f"- `{name}`" for name in self.data["changed_files"])
        if self.data.get("export_error"):
            lines.extend(["", f"补丁导出失败：{self.data['export_error']}"])
        if self.data.get("runtime_run_id"):
            lines.extend(
                [
                    "",
                    f"运行时 run_id：{self.data['runtime_run_id']}",
                    f"Trace：{self.data['repo_path']}/.agent/runs/{self.data['runtime_run_id']}/trace.jsonl",
                ]
            )
        recovery = self.data.get("recovery") or {}
        if recovery:
            lines.extend(
                [
                    "",
                    f"恢复/取消：{recovery['status']}（{recovery['stage']}）",
                    f"原因：{recovery['reason_code'] or '无阻断原因'}",
                    f"下一步：{recovery['guidance']}",
                    f"清理已确认：{recovery['cleanup_confirmed']}；"
                    f"恢复涉及的 Plan 副作用核验：{recovery['effects_verified']}",
                ]
            )
            lines.extend(
                f"- 资源 {r['resource_id']}：{r['reason_code'] or r['status']}"
                for r in recovery["blocking_resources"]
            )
        lines.extend(["", "## 进度", ""])
        lines.extend(f"- {event['phase']}：{event['message']}" for event in self.data["events"])
        (self.output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

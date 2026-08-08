"""LLM-as-Judge eval 变体（V1.4-Bonus6b）。

用 light_client 评估修复质量，输出 score(0-10) + reason，
与 patch_precision 对照。

Usage::

    client = JudgeClient(light_client)
    score, reason = client.evaluate(issue, actual_patch)
"""

from __future__ import annotations

import hashlib
import json

_JUDGE_PROMPT = """Evaluate the following code fix on a scale of 0-10.

**Issue description:**
{issue}

**Applied patch (unified diff):**
{patch}

Rate how well this fix addresses the issue. Consider:
- Correctness: does it fix the root cause?
- Completeness: are edge cases handled?
- Minimality: is the change as small as possible?

Output ONLY a JSON object with "score" (0-10) and "reason" (one short sentence):"""


class JudgeClient:
    """LLM-as-Judge：用 light_client 评分修复质量。"""

    RUBRIC_VERSION = "1.0"

    def __init__(self, model_client, *, model_name: str = "", rubric_version: str = RUBRIC_VERSION):
        self._client = model_client
        self.model_name = model_name
        self.rubric_version = rubric_version

    @property
    def metadata(self) -> dict[str, str]:
        return {
            "model": self.model_name,
            "rubric_version": self.rubric_version,
            "prompt_hash": hashlib.sha256(_JUDGE_PROMPT.encode("utf-8")).hexdigest()[:20],
        }

    def evaluate(self, issue: str, patch: str) -> tuple[int, str]:
        """评估修复质量。

        Args:
            issue: 原始 issue 描述。
            patch: 实际应用的 unified diff。

        Returns:
            (score, reason) — score 为 0-10 整数，reason 为简短评语。
        """
        if not patch or not issue:
            return 0, "no patch or issue to evaluate"

        prompt = _JUDGE_PROMPT.format(
            issue=issue[:800],
            patch=patch[:1200],
        )
        try:
            raw = self._client.complete(prompt, max_new_tokens=256)
        except Exception:
            return 0, "judge model call failed"

        score, reason = _parse_judge_response(raw)
        return score, reason

    @staticmethod
    def compare_with_precision(judge_score: int, patch_precision: float) -> str:
        """对照 judge 评分与机械 patch_precision。

        Returns:
            "aligned" | "judge_higher" | "judge_lower"
        """
        precision_score = int(round(patch_precision * 10))
        if abs(judge_score - precision_score) <= 1:
            return "aligned"
        return "judge_higher" if judge_score > precision_score else "judge_lower"


def _parse_judge_response(raw: str) -> tuple[int, str]:
    """从 LLM 输出中提取 score + reason。"""
    text = raw.strip()
    # 尝试 JSON
    try:
        data = json.loads(text)
        return int(data.get("score", 0)), str(data.get("reason", ""))
    except (json.JSONDecodeError, ValueError):
        pass
    # 尝试从 markdown 代码块中提取
    import re

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            return int(data.get("score", 0)), str(data.get("reason", ""))
        except (json.JSONDecodeError, ValueError):
            pass
    # fallback: 尝试找数字
    m = re.search(r"(\d+)\s*/\s*10", text)
    if m:
        return int(m.group(1)), text[:200]
    return 0, text[:200]

"""Skill catalog, matching, and prompt helpers."""

from src.skills.catalog import SkillCatalog, SkillCatalogError, get_default_catalog
from src.skills.composition import SkillComposer, SkillCompositionResult, SkillStep
from src.skills.contract import (
    CanonicalSkillSpec,
    SideEffectLevel,
    SkillBudgetProfile,
    SkillKind,
    SkillLifecycle,
    SkillScope,
    SkillTrust,
)
from src.skills.decision import CanonicalSkillDecision, build_canonical_skill_decision
from src.skills.executable_spec import ExecutableSkillSpec
from src.skills.execution import SkillExecutionGateway, execute_skill
from src.skills.fallback import (
    SkillFallback,
    apply_skill_fallback,
    resolve_skill_fallback,
    skill_matched_trace_payload,
)
from src.skills.feedback import (
    SkillFeedbackLedger,
    SkillUsageEvent,
    SkillUsageOutcome,
    SkillUsageStage,
)
from src.skills.invocation import (
    SkillErrorCode,
    SkillExecutionResult,
    SkillInvocation,
    SkillInvocationStatus,
)
from src.skills.matcher import match_skill
from src.skills.models import MatchedSkill, SkillSpec
from src.skills.prompt import (
    SkillHintRole,
    format_skill_hint,
    format_skill_hint_block,
    format_skill_hint_for_plan,
    format_skill_miss_hint,
)
from src.skills.registry import (
    CanonicalSkillRegistry,
    SkillRegistry,
    get_default_executable_registry,
)
from src.skills.resolve import resolve_skill_for_plan
from src.skills.router import RouteDecision, SkillRouter, route_executable_skill
from src.skills.validate import SkillValidationIssue, SkillValidationReport, validate_directory

__all__ = [
    "CanonicalSkillDecision",
    "CanonicalSkillRegistry",
    "CanonicalSkillSpec",
    "ExecutableSkillSpec",
    "MatchedSkill",
    "RouteDecision",
    "SkillCatalog",
    "SkillCatalogError",
    "SkillFallback",
    "SideEffectLevel",
    "SkillBudgetProfile",
    "SkillComposer",
    "SkillCompositionResult",
    "SkillErrorCode",
    "SkillExecutionGateway",
    "SkillExecutionResult",
    "SkillFeedbackLedger",
    "SkillInvocation",
    "SkillInvocationStatus",
    "SkillKind",
    "SkillLifecycle",
    "SkillRegistry",
    "SkillRouter",
    "SkillScope",
    "SkillSpec",
    "SkillStep",
    "SkillTrust",
    "SkillUsageEvent",
    "SkillUsageOutcome",
    "SkillUsageStage",
    "SkillValidationIssue",
    "SkillValidationReport",
    "SkillHintRole",
    "apply_skill_fallback",
    "build_canonical_skill_decision",
    "get_default_executable_registry",
    "resolve_skill_fallback",
    "resolve_skill_for_plan",
    "route_executable_skill",
    "execute_skill",
    "skill_matched_trace_payload",
    "format_skill_hint",
    "format_skill_hint_block",
    "format_skill_hint_for_plan",
    "format_skill_miss_hint",
    "get_default_catalog",
    "match_skill",
    "validate_directory",
]

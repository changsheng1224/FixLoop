"""Skill YAML validation (L1 pydantic + L2 catalog semantics)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from src.skills.models import SkillSpec

IssueLevel = Literal["error", "warning"]


@dataclass(frozen=True)
class SkillValidationIssue:
    level: IssueLevel
    path: str
    field: str
    message: str

    def format_line(self) -> str:
        prefix = "ERROR" if self.level == "error" else "WARN"
        loc = f"{self.path}"
        if self.field:
            loc = f"{self.path}: {self.field}"
        return f"{prefix} {loc}: {self.message}"


@dataclass
class SkillValidationReport:
    issues: list[SkillValidationIssue] = field(default_factory=list)
    skill_count: int = 0
    specs: tuple[SkillSpec, ...] = ()

    @property
    def errors(self) -> list[SkillValidationIssue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[SkillValidationIssue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary_line(self) -> str:
        return f"Summary: {len(self.errors)} error(s), {len(self.warnings)} warning(s)"


def _load_specs(directory: Path) -> tuple[list[tuple[Path, SkillSpec]], list[SkillValidationIssue]]:
    if not directory.is_dir():
        return [], [
            SkillValidationIssue(
                level="error",
                path=str(directory),
                field="",
                message="skills directory not found",
            )
        ]

    specs: list[tuple[Path, SkillSpec]] = []
    issues: list[SkillValidationIssue] = []
    for yaml_file in sorted(directory.glob("*.yaml")):
        try:
            raw = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        except Exception as exc:
            issues.append(
                SkillValidationIssue(
                    level="error",
                    path=str(yaml_file),
                    field="",
                    message=f"failed to read YAML: {exc}",
                )
            )
            continue
        if not isinstance(raw, dict):
            issues.append(
                SkillValidationIssue(
                    level="error",
                    path=str(yaml_file),
                    field="",
                    message="skill file must be a mapping",
                )
            )
            continue
        try:
            spec = SkillSpec.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(part) for part in err.get("loc", ()))
                issues.append(
                    SkillValidationIssue(
                        level="error",
                        path=str(yaml_file),
                        field=loc,
                        message=str(err.get("msg", exc)),
                    )
                )
            continue
        specs.append((yaml_file, spec))
    return specs, issues


def validate_catalog(specs: list[tuple[Path, SkillSpec]]) -> list[SkillValidationIssue]:
    """Cross-file semantic checks."""
    issues: list[SkillValidationIssue] = []
    seen_names: dict[str, Path] = {}
    trigger_index: dict[str, list[tuple[Path, str]]] = {}

    for yaml_file, spec in specs:
        path_str = str(yaml_file)
        if spec.name in seen_names:
            issues.append(
                SkillValidationIssue(
                    level="error",
                    path=path_str,
                    field="name",
                    message=f"duplicate skill name {spec.name!r} (also in {seen_names[spec.name]})",
                )
            )
        else:
            seen_names[spec.name] = yaml_file

        stem = yaml_file.stem
        if stem != spec.name:
            issues.append(
                SkillValidationIssue(
                    level="warning",
                    path=path_str,
                    field="name",
                    message=f"filename stem {stem!r} != name {spec.name!r}",
                )
            )

        if not spec.example_issue.strip():
            issues.append(
                SkillValidationIssue(
                    level="warning",
                    path=path_str,
                    field="example_issue",
                    message="empty example_issue",
                )
            )
        if not spec.example_patch.strip():
            issues.append(
                SkillValidationIssue(
                    level="warning",
                    path=path_str,
                    field="example_patch",
                    message="empty example_patch",
                )
            )

        trigger_index.setdefault(spec.trigger_pattern, []).append((yaml_file, spec.name))

    for pattern, entries in trigger_index.items():
        if len(entries) > 1:
            names = ", ".join(name for _, name in entries)
            for yaml_file, _ in entries:
                issues.append(
                    SkillValidationIssue(
                        level="warning",
                        path=str(yaml_file),
                        field="trigger_pattern",
                        message=f"identical trigger_pattern shared with: {names}",
                    )
                )
    return issues


def validate_directory(directory: Path) -> SkillValidationReport:
    """Validate all skill YAML files under *directory*."""
    specs_with_paths, issues = _load_specs(directory)
    if specs_with_paths:
        issues.extend(validate_catalog(specs_with_paths))
    specs = tuple(spec for _, spec in specs_with_paths)
    return SkillValidationReport(issues=issues, skill_count=len(specs), specs=specs)


def format_report(report: SkillValidationReport, *, directory: Path) -> str:
    lines: list[str] = []
    if report.ok and not report.warnings:
        lines.append(f"OK {report.skill_count} skill(s) in {directory}")
        return "\n".join(lines)

    if report.ok:
        lines.append(f"OK {report.skill_count} skill(s) in {directory} (with warnings)")
    else:
        lines.append(f"FAILED validation for {directory}")

    for issue in report.issues:
        lines.append(issue.format_line())
    lines.append(report.summary_line())
    return "\n".join(lines)

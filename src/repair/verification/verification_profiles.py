"""Manifest-driven verification profiles for common language ecosystems."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class VerificationStep:
    name: str
    command: tuple[str, ...]
    phase: str
    timeout_s: int = 120


@dataclass(frozen=True)
class VerificationProfile:
    language: str
    ecosystem: str
    manifests: tuple[str, ...]
    static_steps: tuple[VerificationStep, ...] = field(default_factory=tuple)
    test_steps: tuple[VerificationStep, ...] = field(default_factory=tuple)


_PROFILES = (
    VerificationProfile(
        "python", "pytest", ("pyproject.toml", "pytest.ini", "setup.cfg"),
        test_steps=(VerificationStep("pytest", ("pytest",), "target_tests"),),
    ),
    VerificationProfile(
        "javascript", "npm", ("package.json",),
        static_steps=(VerificationStep("node_check", ("node", "--check"), "static"),),
        test_steps=(VerificationStep("npm_test", ("npm", "test", "--"), "target_tests"),),
    ),
    VerificationProfile(
        "typescript", "npm", ("package.json", "tsconfig.json"),
        static_steps=(VerificationStep("tsc", ("tsc", "--noEmit"), "static"),),
        test_steps=(VerificationStep("npm_test", ("npm", "test", "--"), "target_tests"),),
    ),
    VerificationProfile(
        "java", "maven", ("pom.xml",),
        test_steps=(VerificationStep("maven_test", ("mvn", "test", "-q"), "target_tests"),),
    ),
    VerificationProfile(
        "java", "gradle", ("build.gradle", "build.gradle.kts"),
        test_steps=(VerificationStep("gradle_test", ("gradle", "test"), "target_tests"),),
    ),
    VerificationProfile(
        "go", "go", ("go.mod",),
        static_steps=(VerificationStep("go_vet", ("go", "vet", "./..."), "static"),),
        test_steps=(VerificationStep("go_test", ("go", "test", "./..."), "target_tests"),),
    ),
    VerificationProfile(
        "rust", "cargo", ("Cargo.toml",),
        static_steps=(VerificationStep("cargo_check", ("cargo", "check"), "static"),),
        test_steps=(VerificationStep("cargo_test", ("cargo", "test"), "target_tests"),),
    ),
)


def select_verification_profile(
    repo_root: str | Path, language: str = "python"
) -> VerificationProfile | None:
    root = Path(repo_root)
    normalized = (language or "python").lower()
    candidates = [p for p in _PROFILES if p.language == normalized]
    for profile in candidates:
        if any((root / manifest).is_file() for manifest in profile.manifests):
            return profile
    return candidates[0] if candidates else None

#!/usr/bin/env bash
# Sync obra/superpowers skills into .cursor/skills/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VENDOR=".cursor/skills/_vendor/superpowers"
SKILLS_ROOT=".cursor/skills"

rm -rf "$VENDOR"
mkdir -p "$(dirname "$VENDOR")"
git clone --depth 1 --filter=blob:none --sparse https://github.com/obra/superpowers.git "$VENDOR"
(
  cd "$VENDOR"
  git sparse-checkout set skills
)

UPSTREAM=(
  brainstorming dispatching-parallel-agents executing-plans
  finishing-a-development-branch receiving-code-review requesting-code-review
  subagent-driven-development systematic-debugging test-driven-development
  using-git-worktrees using-superpowers verification-before-completion
  writing-plans writing-skills
)

for name in "${UPSTREAM[@]}"; do
  src="$VENDOR/skills/$name"
  dest="$SKILLS_ROOT/$name"
  [[ -d "$src" ]] || { echo "Missing upstream skill: $name" >&2; exit 1; }
  rm -rf "$dest"
  cp -R "$src" "$dest"
  echo "Updated $name"
done

rm -rf "$VENDOR"
echo "Done. FixLoop skill fixloop-bonus-superpowers was not overwritten."

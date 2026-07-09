# Sync obra/superpowers skills into .cursor/skills/
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$Vendor = Join-Path $Root ".cursor/skills/_vendor/superpowers"
$SkillsRoot = Join-Path $Root ".cursor/skills"

if (Test-Path $Vendor) { Remove-Item -Recurse -Force $Vendor }
New-Item -ItemType Directory -Force -Path (Split-Path $Vendor) | Out-Null

git clone --depth 1 --filter=blob:none --sparse https://github.com/obra/superpowers.git $Vendor
Push-Location $Vendor
git sparse-checkout set skills
Pop-Location

$UpstreamSkills = @(
    "brainstorming", "dispatching-parallel-agents", "executing-plans",
    "finishing-a-development-branch", "receiving-code-review", "requesting-code-review",
    "subagent-driven-development", "systematic-debugging", "test-driven-development",
    "using-git-worktrees", "using-superpowers", "verification-before-completion",
    "writing-plans", "writing-skills"
)

foreach ($name in $UpstreamSkills) {
    $src = Join-Path $Vendor "skills/$name"
    $dest = Join-Path $SkillsRoot $name
    if (-not (Test-Path $src)) { throw "Missing upstream skill: $name" }
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Copy-Item -Recurse $src $dest
    Write-Host "Updated $name"
}

Remove-Item -Recurse -Force $Vendor
Write-Host "Done. FixLoop skill fixloop-bonus-superpowers was not overwritten."

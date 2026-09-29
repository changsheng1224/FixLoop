[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$RunId,

    [string]$SourceRepoCache = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

$protocolPath = Join-Path $repoRoot "configs/swebench/lite_dev5_r16_protocol.json"
if (-not (Test-Path -LiteralPath $protocolPath -PathType Leaf)) {
    throw "Missing frozen protocol: $protocolPath"
}
$protocol = Get-Content -LiteralPath $protocolPath -Raw | ConvertFrom-Json
if ($protocol.protocol_id -ne "swebench-lite-dev5-r16") {
    throw "Unexpected protocol_id: $($protocol.protocol_id)"
}
if ($protocol.official_harness) {
    throw "Frozen Dev5 protocol must not enable the official harness"
}
if (-not $protocol.verifier.enabled -or -not $protocol.verifier.require_sandbox) {
    throw "Frozen Dev5 protocol requires the Docker Verifier"
}
if ($protocol.verifier.image -ne "repair-agent/python-repair") {
    throw "Unexpected frozen verifier image: $($protocol.verifier.image)"
}
if (-not $protocol.critic.enabled -or $protocol.critic.mode -ne "rules_first") {
    throw "Frozen Dev5 protocol requires Critic mode rules_first"
}
if ($protocol.patcher_projection -ne "public_problem_only") {
    throw "Frozen Dev5 protocol requires public_problem_only Patcher input"
}

$instancesPath = Join-Path $repoRoot $protocol.instances_jsonl
if (-not (Test-Path -LiteralPath $instancesPath -PathType Leaf)) {
    throw "Missing frozen instances: $instancesPath"
}
$actualHash = (Get-FileHash -LiteralPath $instancesPath -Algorithm SHA256).Hash.ToLowerInvariant()
$expectedHash = ([string]$protocol.instances_sha256).ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "Frozen instances hash mismatch: expected=$expectedHash actual=$actualHash"
}

if (-not $SourceRepoCache) {
    $SourceRepoCache = Join-Path $repoRoot $protocol.source_repo_cache
}
$SourceRepoCache = [IO.Path]::GetFullPath($SourceRepoCache)
if (-not (Test-Path -LiteralPath $SourceRepoCache -PathType Container)) {
    throw "Missing R16 source repository cache: $SourceRepoCache"
}

$dockerVersion = & docker version --format '{{.Server.Version}}' 2>&1
if ($LASTEXITCODE -ne 0 -or -not $dockerVersion) {
    throw "Docker daemon is not accessible. Run this protocol in the R16 execution context. $dockerVersion"
}
& docker image inspect ([string]$protocol.verifier.image) *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Required verifier image is missing: $($protocol.verifier.image)"
}

$workRoot = Join-Path $repoRoot "artifacts/swebench_repos_$RunId"
$outputDir = Join-Path $repoRoot "artifacts/swebench_lite_dev_live_${RunId}_run"
if ((Test-Path -LiteralPath $workRoot) -or (Test-Path -LiteralPath $outputDir)) {
    throw "RunId already exists; choose a new RunId. work=$workRoot output=$outputDir"
}
New-Item -ItemType Directory -Path $workRoot | Out-Null
New-Item -ItemType Directory -Path $outputDir | Out-Null

$instances = @{}
Get-Content -LiteralPath $instancesPath | ForEach-Object {
    if ($_.Trim()) {
        $row = $_ | ConvertFrom-Json
        $instances[$row.instance_id] = $row
    }
}

$safeDirectories = @()
foreach ($instanceId in $protocol.instance_ids) {
    if (-not $instances.ContainsKey($instanceId)) {
        throw "Frozen instance missing from JSONL: $instanceId"
    }
    $row = $instances[$instanceId]
    $source = Join-Path $SourceRepoCache $instanceId
    $destination = Join-Path $workRoot $instanceId
    if (-not (Test-Path -LiteralPath (Join-Path $source ".git") -PathType Container)) {
        throw "Source cache is not a git repository: $source"
    }

    & git clone --quiet --no-hardlinks $source $destination
    if ($LASTEXITCODE -ne 0) {
        throw "git clone failed: $instanceId"
    }
    & git -C $destination checkout --quiet --force ([string]$row.base_commit)
    if ($LASTEXITCODE -ne 0) {
        throw "git checkout failed: $instanceId $($row.base_commit)"
    }
    $head = (& git -C $destination rev-parse HEAD).Trim()
    $status = (& git -C $destination status --porcelain=v1 --untracked-files=all) -join "`n"
    if ($head -ne $row.base_commit -or $status) {
        throw "Baseline preflight failed: $instanceId head=$head dirty=$([bool]$status)"
    }
    $safeDirectories += [IO.Path]::GetFullPath($destination)
}

$env:GIT_CONFIG_COUNT = [string]$safeDirectories.Count
for ($index = 0; $index -lt $safeDirectories.Count; $index++) {
    [Environment]::SetEnvironmentVariable("GIT_CONFIG_KEY_$index", "safe.directory", "Process")
    [Environment]::SetEnvironmentVariable(
        "GIT_CONFIG_VALUE_$index",
        $safeDirectories[$index],
        "Process"
    )
}

Copy-Item -LiteralPath $protocolPath -Destination (Join-Path $outputDir "protocol.json")
Copy-Item -LiteralPath $instancesPath -Destination (Join-Path $outputDir "instances.jsonl")

# Freeze evaluator behavior even when the caller has conflicting environment variables.
$env:FIXLOOP_CRITIC = "1"
$env:FIXLOOP_CRITIC_MODE = [string]$protocol.critic.mode
$env:FIXLOOP_PROGRESS_JSONL = Join-Path $outputDir "progress.jsonl"

$arguments = @(
    "-m", "src.benchmark.swebench",
    "--provider", [string]$protocol.provider,
    "--model", [string]$protocol.model,
    "--model-name", [string]$protocol.model_name,
    "--instances-jsonl", $instancesPath,
    "--instances-sha256", $expectedHash,
    "--skip-clone",
    "--work-root", $workRoot,
    "--output-dir", $outputDir,
    "--max-retries", [string]$protocol.max_retries,
    "--repair-timeout-s", [string]$protocol.repair_timeout_s,
    "--max-workers", [string]$protocol.max_workers,
    "--require-verifier-sandbox",
    "--instance-ids"
)
$arguments += @($protocol.instance_ids | ForEach-Object { [string]$_ })

$commandRecord = "python " + (($arguments | ForEach-Object { '"' + $_ + '"' }) -join " ")
Set-Content -LiteralPath (Join-Path $outputDir "command.txt") -Value $commandRecord -Encoding utf8
Write-Host "[fixed-dev5] protocol=$($protocol.protocol_id) run=$RunId"
Write-Host "[fixed-dev5] work=$workRoot"
Write-Host "[fixed-dev5] output=$outputDir"

& python @arguments 2>&1 | Tee-Object -FilePath (Join-Path $outputDir "run.log")
$runnerExitCode = $LASTEXITCODE
exit $runnerExitCode

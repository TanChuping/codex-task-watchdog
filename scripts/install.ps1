[CmdletBinding()]
param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$Python = 'python',
    [switch]$Enable,
    [switch]$InstallStartup
)

$ErrorActionPreference = 'Stop'
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $RepositoryRoot 'skills\codex-watchdog'
$Destination = Join-Path $CodexHome 'skills\codex-watchdog'

if (-not (Test-Path -LiteralPath (Join-Path $Source 'SKILL.md'))) {
    throw "Skill source not found: $Source"
}

New-Item -ItemType Directory -Path $Destination -Force | Out-Null

$Files = @(
    'SKILL.md',
    'agents\openai.yaml',
    'references\manifest.schema.json',
    'references\protocol.md',
    'references\recovery-manifest.schema.json',
    'references\timeout-policy.md',
    'scripts\codex_watchdog.py',
    'scripts\check_thread_health.py',
    'scripts\test_codex_watchdog.py'
)

foreach ($RelativePath in $Files) {
    $From = Join-Path $Source $RelativePath
    $To = Join-Path $Destination $RelativePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $To) -Force | Out-Null
    Copy-Item -LiteralPath $From -Destination $To -Force
}

$Watchdog = Join-Path $Destination 'scripts\codex_watchdog.py'
Write-Host "Installed Codex skill at: $Destination"
Write-Host 'Monitoring and startup settings were not changed by the copy step.'

if ($Enable) {
    & $Python $Watchdog enable
    if ($LASTEXITCODE -ne 0) { throw 'Watchdog enable failed.' }
}

if ($InstallStartup) {
    & $Python $Watchdog install
    if ($LASTEXITCODE -ne 0) { throw 'Startup installation failed.' }
}

if (-not $Enable) {
    Write-Host "To enable later: $Python `"$Watchdog`" enable"
}
if (-not $InstallStartup) {
    Write-Host "To preview logon startup: $Python `"$Watchdog`" install --dry-run"
}

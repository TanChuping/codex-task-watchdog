[CmdletBinding()]
param(
    [string]$CodexHome = $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }),
    [string]$Python = 'python',
    [switch]$RemoveSkill
)

$ErrorActionPreference = 'Stop'
$Skill = Join-Path $CodexHome 'skills\codex-watchdog'
$Watchdog = Join-Path $Skill 'scripts\codex_watchdog.py'

if (Test-Path -LiteralPath $Watchdog) {
    & $Python $Watchdog disable
    if ($LASTEXITCODE -ne 0) { throw 'Watchdog disable failed.' }
    & $Python $Watchdog uninstall
    if ($LASTEXITCODE -ne 0) { throw 'Startup removal failed.' }
} else {
    Write-Host "Installed watchdog script was not found: $Watchdog"
}

if ($RemoveSkill -and (Test-Path -LiteralPath $Skill)) {
    $ResolvedCodexHome = (Resolve-Path -LiteralPath $CodexHome).Path
    $ResolvedSkill = (Resolve-Path -LiteralPath $Skill).Path
    $ExpectedSkill = Join-Path $ResolvedCodexHome 'skills\codex-watchdog'
    if ($ResolvedSkill -ne $ExpectedSkill) {
        throw "Refusing to remove unexpected path: $ResolvedSkill"
    }
    Remove-Item -LiteralPath $ResolvedSkill -Recurse -Force
    Write-Host "Removed skill files: $ResolvedSkill"
}

Write-Host "Watchdog runtime evidence was preserved at: $(Join-Path $CodexHome 'watchdog')"

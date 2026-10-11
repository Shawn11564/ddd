<#
.SYNOPSIS
  Install the ddd skill by copying skills/ddd into an agent's skills folder.

.EXAMPLE
  ./install.ps1                    # auto: Claude Code and/or Codex, whichever is present
  ./install.ps1 -Target claude     # ~/.claude/skills/ddd, plus /ddd:<mode> commands in ~/.claude/commands/ddd
  ./install.ps1 -Target codex      # ~/.agents/skills/ddd  (Codex and other Agent Skills hosts)
  ./install.ps1 -Target project    # .\.claude\skills\ddd (+ commands) and .\.agents\skills\ddd in the current repo
  ./install.ps1 -Dir C:\path       # C:\path\ddd (skill only)

  From the web (clones the repo to a temp folder):
  irm https://raw.githubusercontent.com/Shawn11564/ddd/main/install.ps1 | iex
#>
param(
  [ValidateSet('auto', 'claude', 'codex', 'agents', 'project')]
  [string]$Target = 'auto',
  [string]$Dir = ''
)
$ErrorActionPreference = 'Stop'

$RepoUrl = if ($env:DDD_REPO_URL) { $env:DDD_REPO_URL } else { 'https://github.com/Shawn11564/ddd.git' }
$tmp = $null
$here = if ($PSScriptRoot) { $PSScriptRoot } else { '' }
if ($here -and (Test-Path (Join-Path $here 'skills/ddd/SKILL.md'))) {
  $src = Join-Path $here 'skills/ddd'
  $cmds = Join-Path $here 'commands'
} else {
  if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'git is required to download ddd' }
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ('ddd-' + [Guid]::NewGuid().ToString('N'))
  git clone --quiet --depth 1 $RepoUrl (Join-Path $tmp 'ddd')
  if ($LASTEXITCODE -ne 0) { throw 'git clone failed' }
  $src = Join-Path $tmp 'ddd/skills/ddd'
  $cmds = Join-Path $tmp 'ddd/commands'
}

function Install-To([string]$parent) {
  $dest = Join-Path $parent 'ddd'
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  $action = 'installed'
  if (Test-Path $dest) { Remove-Item -Recurse -Force $dest; $action = 'updated' }
  Copy-Item -Recurse -Path $src -Destination $dest
  Get-ChildItem -Path $dest -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
  Write-Host "$action $dest"
}

# Claude Code: one command per mode (/ddd:auto, /ddd:status, ...) so the modes
# show up in the slash-command menu. Overwrites only ddd's own files.
function Install-Commands([string]$parent) {
  $dest = Join-Path $parent 'ddd'
  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  Copy-Item -Path (Join-Path $cmds '*.md') -Destination $dest -Force
  Write-Host "installed commands in $dest"
}

try {
  $homeDir = [Environment]::GetFolderPath('UserProfile')
  if ($Dir) {
    Install-To $Dir
  } else {
    switch ($Target) {
      'claude' { Install-To (Join-Path $homeDir '.claude/skills'); Install-Commands (Join-Path $homeDir '.claude/commands') }
      { $_ -in 'codex', 'agents' } { Install-To (Join-Path $homeDir '.agents/skills') }
      'project' { Install-To '.claude/skills'; Install-Commands '.claude/commands'; Install-To '.agents/skills' }
      'auto' {
        $any = $false
        if (Test-Path (Join-Path $homeDir '.claude')) {
          Install-To (Join-Path $homeDir '.claude/skills'); Install-Commands (Join-Path $homeDir '.claude/commands'); $any = $true
        }
        if ((Test-Path (Join-Path $homeDir '.codex')) -or (Test-Path (Join-Path $homeDir '.agents'))) {
          Install-To (Join-Path $homeDir '.agents/skills'); $any = $true
        }
        if (-not $any) { Install-To (Join-Path $homeDir '.claude/skills'); Install-Commands (Join-Path $homeDir '.claude/commands') }
      }
    }
  }
} finally {
  if ($tmp -and (Test-Path $tmp)) { Remove-Item -Recurse -Force $tmp }
}

if (-not ((Get-Command python -ErrorAction SilentlyContinue) -or (Get-Command py -ErrorAction SilentlyContinue))) {
  Write-Warning 'ddd needs Python 3.8+ on PATH (python or py)'
}
Write-Host 'Start a new agent session, then run /ddd or /ddd:<mode> (Claude Code) or ask for "ddd" (Codex).'

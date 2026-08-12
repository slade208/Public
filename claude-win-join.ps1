<#
  Bootstrap a Windows box into a Claude Code seat on the shared knowledge tree.

      irm operations.dev/claude-win-join.ps1 | iex

  Piping is fine here, unlike on Linux: Invoke-Expression evaluates the script
  in the current session, so the console stays attached and the interactive
  logins work.

  Idempotent — every step checks first, so re-running after a failure is safe.
  Full docs: github.com/slade208/jarvis-agent
             docs/runbooks/join-a-seat-to-shared-knowledge.md
#>

$ErrorActionPreference = 'Stop'

$Tree = if ($env:AGENT_NOTES_DIR) { $env:AGENT_NOTES_DIR } else { Join-Path $HOME 'agent-notes' }
$Repo = if ($env:AGENT_NOTES_REPO) { $env:AGENT_NOTES_REPO } else { 'https://github.com/slade208/agent-notes.git' }
$Seat = if ($env:AGENT_NOTES_SEAT) { $env:AGENT_NOTES_SEAT } else { $env:COMPUTERNAME }

function Say  ($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Note ($m) { Write-Host "    $m" }
function Have ($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }

# ------------------------------------------------------------------ packages
Say 'Checking prerequisites'
$need = @()
if (-not (Have git))  { $need += 'Git.Git' }
if (-not (Have gh))   { $need += 'GitHub.cli' }
if (-not (Have node)) { $need += 'OpenJS.NodeJS.LTS' }
else {
    $major = ((node -v) -replace '^v(\d+).*', '$1') -as [int]
    if ($major -lt 18) { $need += 'OpenJS.NodeJS.LTS' }   # Claude Code needs 18+
}

if ($need.Count) {
    if (-not (Have winget)) { throw "winget not available - install these by hand: $($need -join ', ')" }
    Note "installing: $($need -join ', ')"
    foreach ($p in $need) { winget install --id $p --accept-source-agreements --accept-package-agreements -e }
    Note 'installed - if a command is still not found, reopen the terminal so PATH refreshes and re-run'
} else {
    Note 'git, gh and node 18+ all present'
}

# --------------------------------------------------------------- claude code
Say 'Claude Code'
if (Have claude) { Note 'already installed' } else { npm install -g @anthropic-ai/claude-code }

# ---------------------------------------------------------------------- auth
Say 'GitHub access (the knowledge tree is a private repo)'
gh auth status *>$null
if ($LASTEXITCODE -ne 0) {
    Note 'a browser or device code is needed for this step'
    gh auth login
}
gh auth setup-git

# --------------------------------------------------------------------- clone
Say 'Knowledge tree'
if (Test-Path (Join-Path $Tree '.git')) {
    Note "already at $Tree - pulling"
    git -C $Tree pull --rebase --autostash --quiet 2>$null
} else {
    git clone $Repo $Tree
}
Note "$((Get-ChildItem (Join-Path $Tree 'internal') -Filter *.md).Count) facts in the tree"

# ------------------------------------------------------------------- project
Say 'Which project should this seat use?'
$default = 'C:\projects\scratch'
$project = Read-Host "    path [$default]"
if ([string]::IsNullOrWhiteSpace($project)) { $project = $default }
New-Item -ItemType Directory -Force -Path $project | Out-Null
Note "using $project"

# ---------------------------------------------------------------------- join
Say 'Joining this seat to the tree'
$env:AGENT_NOTES_DIR = $Tree
& (Join-Path $Tree 'tooling\join.ps1') -ProjectPath $project -SeatName $Seat -TreeDir $Tree

# --------------------------------------------------------------------- hooks
Say 'Session hooks'
$settingsPath = Join-Path $project '.claude\settings.json'
New-Item -ItemType Directory -Force -Path (Split-Path $settingsPath) | Out-Null
$cfg = @{}
if (Test-Path $settingsPath) {
    try { $cfg = Get-Content $settingsPath -Raw | ConvertFrom-Json -AsHashtable }
    catch {
        $bak = "$settingsPath.bak"
        Move-Item $settingsPath $bak -Force      # never silently discard a broken file
        Note "existing settings.json was invalid; kept it as $(Split-Path $bak -Leaf)"
        $cfg = @{}
    }
}
if (-not $cfg.ContainsKey('hooks')) { $cfg['hooks'] = @{} }
# The hooks are bash scripts; Git for Windows supplies bash.
$treeSh = $Tree -replace '\\', '/'
foreach ($pair in @(@('SessionStart','session-start.sh'), @('Stop','stop.sh'))) {
    $event, $script = $pair
    $cmd = "bash $treeSh/tooling/hooks/$script"
    if (-not $cfg['hooks'].ContainsKey($event)) { $cfg['hooks'][$event] = @() }
    if (($cfg['hooks'][$event] | ConvertTo-Json -Depth 10 -Compress) -match [regex]::Escape($script)) {
        Note "$event`: already wired"
    } else {
        $cfg['hooks'][$event] += @{ hooks = @(@{ type = 'command'; shell = 'bash'; command = $cmd }) }
        Note "$event`: added"
    }
}
$cfg | ConvertTo-Json -Depth 10 | Set-Content $settingsPath -Encoding UTF8

# -------------------------------------------------------------------- finish
Say 'Done - one thing left for you'
Note "1. Log in to Claude Code if you haven't on this box:   claude"
Note "2. Then, in $project, ask it:"
Note '      "what do you remember?"'
Note '   A joined seat lists facts grouped by topic; empty means the link failed.'
Write-Host ''
Note "seat name: $Seat   (set AGENT_NOTES_SEAT to change it)"
Note "tree:      $Tree"
Note 'full docs: github.com/slade208/agent-notes -> JOINING.md'

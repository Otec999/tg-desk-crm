#Requires -Version 5.1
<#
.SYNOPSIS
    TG-Desk CRM - developer shortcuts for Windows (PowerShell 5.1 and 7+).

.DESCRIPTION
    PowerShell equivalent of the root `Makefile`. Runs on BOTH Windows
    PowerShell 5.1 (ships with Windows 10/11) and PowerShell 7+.

    IMPORTANT: PowerShell 5.1 does NOT support the `&&` / `||` operators.
    Every external command below is therefore guarded explicitly with
    `if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }`, so a failure always stops
    the script with the real exit code instead of silently continuing.

.EXAMPLE
    .\make.ps1 up
.EXAMPLE
    .\make.ps1 ps
.EXAMPLE
    .\make.ps1 seed -Reset
.EXAMPLE
    .\make.ps1 api -Port 8001
.EXAMPLE
    Get-Help .\make.ps1 -Detailed

.NOTES
    Blocked by the execution policy? Run it like this instead:
        powershell -NoProfile -ExecutionPolicy Bypass -File .\make.ps1 up
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $Target = 'help',

    # Used by `seed` (drop and recreate) and `api` (ignored).
    [switch] $Reset,

    # Used by `api` only.
    [int] $Port = 8000,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Rest
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
$Root         = $PSScriptRoot
$ComposeFile  = 'docker-compose.dev.yml'
$BackendDir   = Join-Path $Root 'backend'
$FrontendDir  = Join-Path $Root 'frontend'
$WorkerDir    = Join-Path $Root 'telethon_worker'
$VenvDir      = Join-Path $BackendDir '.venv'
$VenvPython   = Join-Path $VenvDir 'Scripts\python.exe'
$Requirements = Join-Path $BackendDir 'requirements.txt'
$EnvFile      = Join-Path $Root '.env'
$EnvExample   = Join-Path $Root '.env.example'

$sep = '  ' + ('-' * 62)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
function Invoke-Step {
    <#
        Runs a native command, echoes it, and fails the whole script with the
        command's real exit code when it fails. This is the 5.1-safe replacement
        for the `&&` operator.
    #>
    param(
        [Parameter(Mandatory)] [string] $FilePath,
        [string[]] $ArgumentList = @()
    )

    Write-Host ("  > {0} {1}" -f $FilePath, ($ArgumentList -join ' ')) -ForegroundColor DarkGray
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("  [make.ps1] FAILED: {0} (exit {1})" -f $FilePath, $LASTEXITCODE) -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

function Test-CommandExists {
    param([Parameter(Mandatory)] [string] $Name)
    return [bool] (Get-Command -Name $Name -ErrorAction SilentlyContinue)
}

function Assert-Command {
    param([Parameter(Mandatory)] [string] $Name, [Parameter(Mandatory)] [string] $Hint)

    if (-not (Test-CommandExists $Name)) {
        Write-Host ("  [make.ps1] '{0}' not found. {1}" -f $Name, $Hint) -ForegroundColor Red
        exit 127
    }
}

function Assert-Directory {
    param([Parameter(Mandatory)] [string] $Path, [Parameter(Mandatory)] [string] $Label)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        Write-Host ("  [make.ps1] Missing directory: {0} ({1})" -f $Path, $Label) -ForegroundColor Red
        exit 1
    }
}

function Ensure-EnvFile {
    <# Idempotent: only copies .env.example -> .env when .env is absent. #>
    if (Test-Path -LiteralPath $EnvFile) { return }

    if (-not (Test-Path -LiteralPath $EnvExample)) {
        Write-Host '  [make.ps1] .env.example not found - cannot create .env' -ForegroundColor Red
        exit 1
    }

    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    Write-Host '  [make.ps1] .env created from .env.example' -ForegroundColor Yellow
}

function Get-VenvPython {
    <# Returns the venv interpreter path, or fails with actionable guidance. #>
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Host "  [make.ps1] backend venv not found. Run '.\make.ps1 backend-install' first." -ForegroundColor Red
        exit 1
    }
    return $VenvPython
}

function Invoke-InDirectory {
    <# Runs a native command inside a directory, then restores the location. #>
    param(
        [Parameter(Mandatory)] [string] $Path,
        [Parameter(Mandatory)] [string] $FilePath,
        [string[]] $ArgumentList = @()
    )

    Assert-Directory -Path $Path -Label 'working directory'
    Push-Location -LiteralPath $Path
    try {
        Invoke-Step -FilePath $FilePath -ArgumentList $ArgumentList
    }
    finally {
        Pop-Location
    }
}

function Invoke-Npm {
    <#
        npm on Windows is a .cmd shim. Going through `cmd /c` from inside the
        target directory works on PowerShell 5.1 and 7 alike (and avoids the
        "running scripts is disabled" ExecutionPolicy error on npm.ps1).
    #>
    param(
        [Parameter(Mandatory)] [string]   $Path,
        [Parameter(Mandatory)] [string[]] $Arguments
    )

    Assert-Command -Name 'npm' -Hint 'Install Node.js 20+ (npm ships with it).'
    if (-not (Test-CommandExists 'cmd')) {
        Write-Host "  [make.ps1] 'cmd' not found - cannot invoke the npm shim." -ForegroundColor Red
        exit 127
    }
    Invoke-InDirectory -Path $Path -FilePath 'cmd' -ArgumentList (@('/c', 'npm') + $Arguments)
}

function Invoke-Compose {
    <# docker compose -f docker-compose.dev.yml <args...>, always from the repo root. #>
    param([Parameter(Mandatory)] [string[]] $Arguments)

    Assert-Command -Name 'docker' -Hint 'Install Docker Desktop, start it, then retry.'
    if (-not (Test-Path -LiteralPath (Join-Path $Root $ComposeFile) -PathType Leaf)) {
        Write-Host ("  [make.ps1] Compose file not found: {0}" -f $ComposeFile) -ForegroundColor Red
        exit 1
    }
    Push-Location -LiteralPath $Root
    try {
        Invoke-Step -FilePath 'docker' -ArgumentList (@('compose', '-f', $ComposeFile) + $Arguments)
    }
    finally {
        Pop-Location
    }
}

function Wait-ComposeHealthy {
    <# Best-effort readiness wait (90 s max); never fails the script. #>
    param([int] $TimeoutSeconds = 90)

    if (-not (Test-CommandExists 'docker')) { return }

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $healthy  = $false

    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $pg = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' tgdesk-postgres 2>$null)
        $rd = (& docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' tgdesk-redis 2>$null)
        if ($pg -eq 'healthy' -and $rd -eq 'healthy') {
            $healthy = $true
            break
        }
        # Containers gone (e.g. `up` failed) - stop waiting.
        if (-not $pg -and -not $rd) { break }
    }

    if ($healthy) {
        Write-Host '  [make.ps1] postgres + redis are healthy' -ForegroundColor Green
    }
    else {
        Write-Host '  [make.ps1] still starting - check with: .\make.ps1 ps' -ForegroundColor Yellow
    }
}

function Show-Help {
    $targets = [ordered]@{
        'up'              = 'Start PostgreSQL 16 + Redis 7 in the background'
        'down'            = 'Stop and remove containers (named volumes / data kept)'
        'logs'            = 'Follow logs of all services (Ctrl+C stops following only)'
        'ps'              = 'Show container status'
        'backend-install' = 'Create backend\.venv and install requirements'
        'seed'            = 'Seed the database (add -Reset to drop and recreate)'
        'api'             = 'Run FastAPI with autoreload on :8000 (use -Port N)'
        'worker'          = 'Run the Telethon worker (python worker.py)'
        'fe-install'      = 'npm install in frontend\'
        'fe'              = 'Run the Vite dev server on :5173'
        'dev'             = 'Start infra and print the 4 terminals to run, in order'
        'build'           = 'Build the frontend and byte-compile the Python sources'
        'init-env'        = 'Create .env from .env.example if missing'
        'help'            = 'Show this help'
    }

    Write-Host ''
    Write-Host '  TG-Desk CRM - make.ps1 targets' -ForegroundColor Cyan
    Write-Host $sep
    foreach ($k in $targets.Keys) {
        Write-Host ('  {0,-16} {1}' -f $k, $targets[$k])
    }
    Write-Host $sep
    Write-Host '  Examples:  .\make.ps1 up'
    Write-Host '             .\make.ps1 seed -Reset'
    Write-Host '             .\make.ps1 api -Port 8001'
    Write-Host ''
}

# --------------------------------------------------------------------------- #
# Targets
# --------------------------------------------------------------------------- #
switch ($Target.ToLowerInvariant()) {

    'help' {
        Show-Help
    }

    'init-env' {
        Ensure-EnvFile
        Write-Host '  [make.ps1] .env ready' -ForegroundColor Green
    }

    # ------------------------------------------------------------------ infra --
    'up' {
        Ensure-EnvFile
        Invoke-Compose -Arguments @('up', '-d')
        Write-Host ''
        Write-Host '  [make.ps1] waiting for containers to report healthy...' -ForegroundColor DarkGray
        Wait-ComposeHealthy
        Invoke-Compose -Arguments @('ps')
    }

    'down' {
        Invoke-Compose -Arguments @('down')
    }

    'logs' {
        Invoke-Compose -Arguments @('logs', '-f', '--tail', '200')
    }

    'ps' {
        Invoke-Compose -Arguments @('ps')
    }

    # ---------------------------------------------------------------- backend --
    'backend-install' {
        if (-not (Test-Path -LiteralPath $Requirements -PathType Leaf)) {
            Write-Host ("  [make.ps1] Requirements file not found: {0}" -f $Requirements) -ForegroundColor Red
            exit 1
        }
        Assert-Command -Name 'python' -Hint 'Install Python 3.11+ and make sure it is on PATH.'

        if (-not (Test-Path -LiteralPath $VenvPython)) {
            Invoke-Step -FilePath 'python' -ArgumentList @('-m', 'venv', $VenvDir)
        }
        else {
            Write-Host ("  [make.ps1] reusing existing venv: {0}" -f $VenvDir) -ForegroundColor DarkGray
        }

        Invoke-Step -FilePath $VenvPython -ArgumentList @('-m', 'pip', 'install', '--upgrade', 'pip')
        Invoke-Step -FilePath $VenvPython -ArgumentList @('-m', 'pip', 'install', '-r', $Requirements)

        Write-Host '  [make.ps1] backend ready -> next: .\make.ps1 seed  then  .\make.ps1 api' -ForegroundColor Green
    }

    'seed' {
        $py = Get-VenvPython
        $seedArgs = @('-m', 'seed')
        if ($Reset) { $seedArgs += '--reset' }
        Invoke-InDirectory -Path $BackendDir -FilePath $py -ArgumentList $seedArgs
    }

    'api' {
        $py = Get-VenvPython
        Invoke-InDirectory -Path $BackendDir -FilePath $py -ArgumentList @(
            '-m', 'uvicorn', 'app.main:app', '--reload', '--port', $Port
        )
    }

    # ----------------------------------------------------------------- worker --
    'worker' {
        $py = Get-VenvPython
        Assert-Directory -Path $WorkerDir -Label 'telethon_worker'
        Invoke-InDirectory -Path $WorkerDir -FilePath $py -ArgumentList @('worker.py')
    }

    # --------------------------------------------------------------- frontend --
    'fe-install' {
        Assert-Directory -Path $FrontendDir -Label 'frontend'
        Invoke-Npm -Path $FrontendDir -Arguments @('install', '--no-fund', '--no-audit')
    }

    'fe' {
        Assert-Directory -Path $FrontendDir -Label 'frontend'
        Invoke-Npm -Path $FrontendDir -Arguments @('run', 'dev')
    }

    # ------------------------------------------------------------------ combo --
    'dev' {
        Ensure-EnvFile
        Invoke-Compose -Arguments @('up', '-d')

        Write-Host ''
        Write-Host '  ============================================================' -ForegroundColor Cyan
        Write-Host '   TG-Desk CRM - run these 4 terminals IN THIS ORDER' -ForegroundColor Cyan
        Write-Host '  ============================================================' -ForegroundColor Cyan
        Write-Host '   1)  .\make.ps1 api       -> FastAPI  http://localhost:8000/health'
        Write-Host '   2)  .\make.ps1 seed      -> tables + demo data (once; -Reset to wipe)'
        Write-Host '   3)  .\make.ps1 worker    -> Telethon worker (needs real TG_API_*)'
        Write-Host '   4)  .\make.ps1 fe        -> Vite     http://localhost:5173'
        Write-Host '  ------------------------------------------------------------'
        Write-Host '   Infra is up:  .\make.ps1 ps  |  logs  |  down'
        Write-Host '  ============================================================' -ForegroundColor Cyan
        Write-Host ''
    }

    # ------------------------------------------------------------------ build --
    'build' {
        if (-not (Test-Path -LiteralPath (Join-Path $FrontendDir 'package.json') -PathType Leaf)) {
            Write-Host '  [make.ps1] frontend\package.json not found - skipping frontend build.' -ForegroundColor Yellow
        }
        else {
            Invoke-Npm -Path $FrontendDir -Arguments @('run', 'build')
        }

        # Byte-compile Python sources (syntax check; no artefacts are committed).
        $py = $null
        if (Test-Path -LiteralPath $VenvPython) {
            $py = $VenvPython
        }
        elseif (Test-CommandExists 'python') {
            $py = 'python'
        }

        if (-not $py) {
            Write-Host '  [make.ps1] No Python interpreter available - skipping compileall.' -ForegroundColor Yellow
            exit 0
        }

        $compileTargets = @()
        foreach ($d in @($BackendDir, $WorkerDir)) {
            if (Test-Path -LiteralPath $d -PathType Container) { $compileTargets += $d }
        }

        if ($compileTargets.Count -eq 0) {
            Write-Host '  [make.ps1] No Python source directories found - skipping compileall.' -ForegroundColor Yellow
        }
        else {
            Invoke-Step -FilePath $py -ArgumentList (@('-m', 'compileall', '-q') + $compileTargets)
        }

        Write-Host '  [make.ps1] build OK' -ForegroundColor Green
    }

    default {
        Write-Host ("  [make.ps1] Unknown target: '{0}'" -f $Target) -ForegroundColor Red
        Show-Help
        exit 2
    }
}

exit 0

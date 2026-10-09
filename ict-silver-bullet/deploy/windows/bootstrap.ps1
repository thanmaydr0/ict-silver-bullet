#requires -Version 5.1
[CmdletBinding()]
param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
Push-Location -LiteralPath $RepoRoot
try {
    foreach ($Package in @('brain', 'executor')) {
        $VenvPython = Join-Path $RepoRoot "$Package\.venv\Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $VenvPython)) {
            Write-Host "Creating $Package virtual environment (Python 3.10+ required)..."
            & $Python -c 'import sys; assert sys.version_info >= (3, 10)'
            if ($LASTEXITCODE -ne 0) { throw 'Python version check failed.' }
            & $Python -m venv (Join-Path $RepoRoot "$Package\.venv")
            if ($LASTEXITCODE -ne 0) { throw "Could not create $Package venv." }
        }
        $Requirements = if ($Package -eq 'brain') {
            @('brain\requirements.txt', 'brain\requirements-ml.txt', 'backtest\requirements.txt')
        } else { @('executor\requirements.txt') }
        Write-Host "Installing requirements into $Package venv..."
        $PipArgs = @('-m', 'pip', 'install')
        foreach ($File in $Requirements) { $PipArgs += @('-r', (Join-Path $RepoRoot $File)) }
        & $VenvPython @PipArgs
        if ($LASTEXITCODE -ne 0) { throw "$Package dependency installation failed." }
    }
    New-Item -ItemType Directory -Path (Join-Path $RepoRoot 'logs') -Force | Out-Null
    & (Join-Path $PSScriptRoot 'build_frontend.ps1')
    $Missing = $false
    foreach ($Package in @('brain', 'executor')) {
        if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot "$Package\.env"))) {
            $Missing = $true
            Write-Warning "Missing $Package\.env. Create it manually using .env.example; this script never creates or fills credentials."
        } else { Write-Host "Found $Package\.env (contents not read or displayed)." }
    }
    Write-Host 'Configure Windows auto-logon for this user and log MT5 into the intended account.'
    Write-Host '.\deploy\windows\install_tasks.ps1 -TerminalPath "C:\Program Files\MetaTrader 5\terminal64.exe"'
    Write-Host 'Reboot, reconnect with RDP, run status.ps1 -Health, and open http://<Elastic IP>:7860.'
    if ($Missing) { Write-Warning 'Create the missing .env files BEFORE installing/starting tasks.' }
} finally { Pop-Location }

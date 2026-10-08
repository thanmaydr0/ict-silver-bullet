#requires -Version 5.1
[CmdletBinding()]
param([switch]$Force, [switch]$DashboardOnly, [string]$RepositoryRoot)
$ErrorActionPreference = 'Stop'
$RepoRoot = if ($RepositoryRoot) { (Resolve-Path -LiteralPath $RepositoryRoot).Path } else {
    (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
}
$DeployDir = Join-Path $RepoRoot 'deploy\windows'
$ExecutorPython = Join-Path $RepoRoot 'executor\.venv\Scripts\python.exe'
$BrainPython = Join-Path $RepoRoot 'brain\.venv\Scripts\python.exe'
$StopOrder = @('ICT-Brain', 'ICT-Executor', 'ICT-Watchdog', 'ICT-Equity', 'ICT-Candles', 'ICT-Dashboard', 'ICT-MT5Terminal')
$StartOrder = @('ICT-MT5Terminal', 'ICT-Candles', 'ICT-Equity', 'ICT-Watchdog', 'ICT-Dashboard', 'ICT-Brain', 'ICT-Executor')

function Assert-NoOpenPositions {
    # Executed ONLY on EC2. Query both actual terminal positions and persisted
    # trades directly; the fault-tolerant getters cannot distinguish failure/empty.
    $Check = @'
import sys
try:
    from executor.config import load_config
    from supabase import create_client
    import MetaTrader5 as mt5
    c = load_config()
    rows = create_client(c.SUPABASE_URL, c.SUPABASE_SERVICE_KEY).table("trades").select("id").eq("status", "open").limit(1).execute().data
    if rows is None:
        raise RuntimeError("missing database response")
    if not mt5.initialize(**({"path": c.MT5_TERMINAL_PATH} if c.MT5_TERMINAL_PATH else {})):
        raise RuntimeError("terminal unavailable")
    account = mt5.account_info()
    if account is None or account.login != c.MT5_LOGIN or account.server != c.MT5_SERVER:
        raise RuntimeError("terminal account mismatch")
    positions = mt5.positions_get()
    if positions is None:
        raise RuntimeError("positions unavailable")
    sys.exit(2 if rows or positions else 0)
except Exception:
    sys.exit(3)
finally:
    if "mt5" in globals():
        mt5.shutdown()
'@
    & $ExecutorPython -c $Check
    $CheckResult = $LASTEXITCODE
    if ($CheckResult -ne 0) {
        $Reason = if ($CheckResult -eq 2) { 'Open positions exist.' } else { 'Position check failed or account could not be verified.' }
        Write-Warning "$Reason Refusing to restart Executor without -Force."
        throw 'Update aborted. Close positions and retry, or explicitly pass -Force after reviewing the risk.'
    }
}
Push-Location -LiteralPath $RepoRoot
try {
    $Branch = & git branch --show-current
    if ($LASTEXITCODE -ne 0 -or $Branch -ne 'main') { throw 'Updates must run on main.' }
    $Dirty = & git status --porcelain
    if ($LASTEXITCODE -ne 0 -or $Dirty) { throw 'Working tree must be clean before updating.' }
    if ($DashboardOnly) {
        if ($Force) { throw '-Force cannot be combined with -DashboardOnly.' }
        & git fetch origin main
        if ($LASTEXITCODE -ne 0) { throw 'Fetch failed.' }
        & git merge-base --is-ancestor HEAD origin/main
        if ($LASTEXITCODE -ne 0) { throw 'Dashboard-only update requires a fast-forward from HEAD to origin/main.' }
        $ChangedFiles = @(& git diff --name-only HEAD origin/main)
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect incoming changes.' }
        # Git root may be the parent of this app directory (the documented clone).
        $GitPrefix = (& git rev-parse --show-prefix).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Could not determine repository layout.' }
        $Allowed = '^(frontend/|brain/dashboard(?:_api|_auth|_data|_models|_dev)?\.py$|scripts/export_dashboard_schema\.py$|tests/brain/test_dashboard[^/]*\.py$|tests/deploy/test_windows_scripts\.py$|deploy/windows/(?:build_frontend|status|update)\.ps1$|deploy/windows/README-deploy\.md$|README\.md$|\.gitignore$)'
        foreach ($File in $ChangedFiles) {
            if (-not $File.StartsWith($GitPrefix) -or $File.Substring($GitPrefix.Length) -notmatch $Allowed) {
                throw "Incoming change $File requires the full guarded update."
            }
        }
        foreach ($Package in @('brain', 'executor')) {
            if (Test-Path -LiteralPath (Join-Path $RepoRoot "logs\$Package.requirements-pending")) { throw 'Pending Python dependency repair requires full update.' }
        }
        Get-ScheduledTask -TaskName 'ICT-Dashboard' -ErrorAction Stop | Out-Null
        # Stop before advancing Python source; failure keeps trading tasks intact.
        Stop-ScheduledTask -TaskName 'ICT-Dashboard'
        $Deadline = (Get-Date).AddSeconds(30)
        while ((Get-ScheduledTask -TaskName 'ICT-Dashboard').State -eq 'Running') {
            if ((Get-Date) -gt $Deadline) { throw 'ICT-Dashboard did not stop.' }
            Start-Sleep -Milliseconds 500
        }
        & git merge --ff-only origin/main
        if ($LASTEXITCODE -ne 0) { throw 'Dashboard-only fast-forward failed.' }
        & (Join-Path $DeployDir 'build_frontend.ps1')
        Start-ScheduledTask -TaskName 'ICT-Dashboard'
        Write-Host 'Dashboard updated. Verify status.ps1 -Health; trading tasks were left running.'
        return
    }
    foreach ($PythonExe in @($ExecutorPython, $BrainPython)) {
        if (-not (Test-Path -LiteralPath $PythonExe)) { throw 'Run bootstrap.ps1 first.' }
    }
    foreach ($Name in $StartOrder) { Get-ScheduledTask -TaskName $Name -ErrorAction Stop | Out-Null }
    if ($Force) { Write-Warning '-Force: open/unknown positions may lose management during this restart.' }
    else { Assert-NoOpenPositions }
    $Requirements = @('brain\requirements.txt', 'brain\requirements-ml.txt', 'backtest\requirements.txt', 'executor\requirements.txt')
    $Before = @{}
    foreach ($File in $Requirements) { $Before[$File] = (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash }
    # Quiesce before changing source/dependencies. Stop producers before consumers.
    foreach ($Name in $StopOrder | Where-Object { $_ -ne 'ICT-MT5Terminal' }) {
        Stop-ScheduledTask -TaskName $Name
        $Deadline = (Get-Date).AddSeconds(30)
        while ((Get-ScheduledTask -TaskName $Name).State -eq 'Running') {
            if ((Get-Date) -gt $Deadline) { throw "$Name did not stop; update aborted." }
            Start-Sleep -Milliseconds 500
        }
    }
    # NOTE: Recheck after quiescing to catch an entry racing the first check.
    # If it fails, leave tasks stopped for operator review; never silently force.
    if (-not $Force) { Assert-NoOpenPositions }
    & git pull origin main
    if ($LASTEXITCODE -ne 0) { throw 'git pull failed. Tasks remain stopped; resolve before restarting.' }
    $Changed = @($Requirements | Where-Object { (Get-FileHash -LiteralPath $_ -Algorithm SHA256).Hash -ne $Before[$_] })
    $LogDir = Join-Path $RepoRoot 'logs'
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
    # Mark BOTH venvs before installing either; a Brain failure must not lose
    # the outstanding Executor change on the next retry.
    foreach ($Package in @('brain', 'executor')) {
        $Files = if ($Package -eq 'brain') { $Requirements[0..2] } else { @($Requirements[3]) }
        if (@($Files | Where-Object { $Changed -contains $_ }).Count -gt 0) {
            Set-Content -LiteralPath (Join-Path $LogDir "$Package.requirements-pending") `
                -Value 'Requirements install pending' -Encoding ASCII
        }
    }
    foreach ($Package in @('brain', 'executor')) {
        $Files = if ($Package -eq 'brain') { $Requirements[0..2] } else { @($Requirements[3]) }
        $Pending = Join-Path $LogDir "$Package.requirements-pending"
        # NOTE: Persist failed installs so a retry still repairs dependencies
        # after Git has already advanced and the next pull changes no files.
        if (Test-Path -LiteralPath $Pending) {
            Write-Host "Requirements changed: reinstalling $Package dependencies..."
            Set-Content -LiteralPath $Pending -Value 'Requirements install pending' -Encoding ASCII
            $PythonExe = if ($Package -eq 'brain') { $BrainPython } else { $ExecutorPython }
            $PipArgs = @('-m', 'pip', 'install')
            foreach ($File in $Files) { $PipArgs += @('-r', (Join-Path $RepoRoot $File)) }
            & $PythonExe @PipArgs
            if ($LASTEXITCODE -ne 0) { throw "$Package install failed. Tasks remain stopped." }
            Remove-Item -LiteralPath $Pending
        } else { Write-Host "$Package requirements unchanged; skipping pip." }
    }
    & (Join-Path $DeployDir 'build_frontend.ps1')
    # Keep a running terminal intact; restarting its GUI is unnecessary for code updates.
    # NOTE: MT5 is started if absent; an existing GUI terminal is never interrupted.
    if (-not (Get-Process -Name terminal64 -ErrorAction SilentlyContinue)) {
        Stop-ScheduledTask -TaskName 'ICT-MT5Terminal'
        Start-ScheduledTask -TaskName 'ICT-MT5Terminal'
        Write-Host 'Waiting 60 seconds for MT5...'
        Start-Sleep -Seconds 60
        if (-not (Get-Process -Name terminal64 -ErrorAction SilentlyContinue)) { throw 'MT5 did not start.' }
    }
    foreach ($Name in $StartOrder | Where-Object { $_ -ne 'ICT-MT5Terminal' }) {
        Start-ScheduledTask -TaskName $Name
        Write-Host "Started $Name"
    }
    Write-Host 'Update complete. Executor started last. Run status.ps1 and verify fresh equity/candles.'
} catch {
    Write-Warning 'If tasks were stopped, they stay stopped after a failed update. Inspect logs/positions before recovery; do not blindly start Executor.'
    throw
} finally { Pop-Location }

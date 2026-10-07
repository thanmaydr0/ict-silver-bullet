#requires -Version 5.1
[CmdletBinding()]
param([string]$TerminalPath, [switch]$Uninstall)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$TaskNames = @('ICT-MT5Terminal', 'ICT-Candles', 'ICT-Executor', 'ICT-Equity', 'ICT-Watchdog', 'ICT-Brain', 'ICT-Dashboard')
if ($Uninstall) {
    foreach ($Name in @('ICT-Brain', 'ICT-Executor', 'ICT-Watchdog', 'ICT-Equity', 'ICT-Candles', 'ICT-Dashboard', 'ICT-MT5Terminal')) {
        if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $Name
            Unregister-ScheduledTask -TaskName $Name -Confirm:$false
            Write-Host "Removed $Name"
        } else { Write-Host "$Name is already absent." }
    }
    return
}
if (-not $TerminalPath -or -not (Test-Path -LiteralPath $TerminalPath -PathType Leaf)) {
    throw 'Pass -TerminalPath with the full path to terminal64.exe.'
}
$TerminalPath = (Resolve-Path -LiteralPath $TerminalPath).Path
foreach ($Package in @('brain', 'executor')) {
    foreach ($Relative in @("$Package\.venv\Scripts\python.exe", "$Package\.env")) {
        if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $Relative))) {
            throw "Missing $Relative. Create .env manually and run bootstrap.ps1 first."
        }
    }
}
$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
# NOTE: Use the documented schema maximum: Count is unsignedByte (255).
# https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-count-restarttype-element
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 255 -StartWhenAvailable `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Modules = @{
    'ICT-Candles' = @('executor', 'executor.mt5_bridge', 'candles')
    'ICT-Executor' = @('executor', 'executor.order_executor', 'executor')
    'ICT-Equity' = @('executor', 'executor.trade_reporter', 'equity')
    'ICT-Watchdog' = @('executor', 'executor.watchdog', 'watchdog')
    'ICT-Brain' = @('brain', 'brain.app', 'brain')
    'ICT-Dashboard' = @('brain', 'brain.dashboard', 'dashboard')
}
function Quote-PSLiteral([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }
foreach ($Name in $TaskNames) {
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
    if ($Name -eq 'ICT-MT5Terminal') {
        $Action = New-ScheduledTaskAction -Execute $TerminalPath -WorkingDirectory $RepoRoot
    } else {
        $Trigger.Delay = 'PT60S'
        $Spec = $Modules[$Name]
        $PythonExe = Join-Path $RepoRoot "$($Spec[0])\.venv\Scripts\python.exe"
        $Stdout = Join-Path $LogDir "$($Spec[2]).out.log"
        $Stderr = Join-Path $LogDir "$($Spec[2]).err.log"
        # PowerShell wrapper waits for Python and propagates its exit code so
        # Scheduler's failure restarts work. Append retains previous startup errors.
        # NOTE: Native stderr in PS 5.1 may include NativeCommandError formatting.
        $Command = '$ErrorActionPreference = ''Continue''; Set-Location -LiteralPath ' + (Quote-PSLiteral $RepoRoot) +
            '; & ' + (Quote-PSLiteral $PythonExe) + ' -u -m ' + $Spec[1] +
            ' 1>> ' + (Quote-PSLiteral $Stdout) + ' 2>> ' + (Quote-PSLiteral $Stderr) + '; exit $LASTEXITCODE'
        $Encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
        $Action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
            -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand $Encoded" -WorkingDirectory $RepoRoot
    }
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings `
        -Principal $Principal -Description 'ICT Silver Bullet: interactive auto-logon session on EC2 Windows.' -Force | Out-Null
    Write-Host "Registered $Name for $User (logon; interactive only)."
}
Write-Host 'Tasks are registered; reboot to verify auto-logon, terminal first, then Python after 60 seconds.'
Write-Host 'Disconnect RDP; NEVER sign out. Use status.ps1 to inspect tasks and startup logs.'

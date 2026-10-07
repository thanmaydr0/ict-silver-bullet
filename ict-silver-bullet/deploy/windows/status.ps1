#requires -Version 5.1
[CmdletBinding()]
param([ValidateRange(1, 200)][int]$Tail = 10)
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$Names = @('ICT-MT5Terminal', 'ICT-Candles', 'ICT-Executor', 'ICT-Equity', 'ICT-Watchdog', 'ICT-Brain', 'ICT-Dashboard')
$Rows = foreach ($Name in $Names) {
    $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($Task) {
        $Info = Get-ScheduledTaskInfo -TaskName $Name
        [pscustomobject]@{ Task = $Name; State = $Task.State; LastRun = $Info.LastRunTime;
            LastResult = ('0x{0:X8}' -f $Info.LastTaskResult) }
    } else { [pscustomobject]@{ Task = $Name; State = 'Not installed'; LastRun = $null; LastResult = $null } }
}
$Rows | Format-Table -AutoSize
Write-Host 'Result 0x00000000 = success; 0x00041301 = still running. Other results need inspection.'
$Terminal = Get-Process -Name terminal64 -ErrorAction SilentlyContinue
if ($Terminal) { Write-Host "terminal64.exe running: PID(s) $($Terminal.Id -join ', ')" }
else { Write-Warning 'terminal64.exe is NOT running.' }
$LogDir = Join-Path $RepoRoot 'logs'
$Logs = @(Get-ChildItem -LiteralPath $LogDir -Filter '*.log' -File -ErrorAction SilentlyContinue)
if (-not $Logs) { Write-Host 'No logs yet.' }
foreach ($Log in $Logs) {
    Write-Host "`n--- $($Log.Name) (last $Tail lines) ---"
    Get-Content -LiteralPath $Log.FullName -Tail $Tail
}
# NOTE: Skip equity-age network queries here. The dashboard reports snapshot
# age and stale data; status.ps1 stays usable when the database is unavailable.

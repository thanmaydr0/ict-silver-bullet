#requires -Version 5.1
[CmdletBinding()]
param([ValidateRange(1, 200)][int]$Tail = 10, [switch]$History,
    [switch]$Health, [ValidateRange(1,65535)][int]$DashboardPort = 7860)
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
if ($Health) {
    try {
        $DashboardHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$DashboardPort/healthz" -TimeoutSec 2
        Write-Host "Dashboard health: $($DashboardHealth.status); Vite build: $($DashboardHealth.build_id); API v$($DashboardHealth.schema_version)"
    } catch { Write-Warning 'Dashboard health endpoint unavailable. Check dashboard.err.log and the configured bind address/port.' }
}
Write-Host 'Result 0x00000000 = success; 0x00041301 = still running; 0xC000013A = console interruption.'
if (@($Rows | Where-Object { $_.LastResult -eq '0xC000013A' -and $_.State -ne 'Running' }).Count -gt 0) {
    Write-Warning 'Tasks report console interruptions and are not Running. Check console closure, Windows sign-out/session policy, manual stops, and task history. This code alone does not identify the cause.'
}
$Terminal = Get-Process -Name terminal64 -ErrorAction SilentlyContinue
if ($Terminal) { Write-Host "terminal64.exe running: PID(s) $($Terminal.Id -join ', ')" }
else { Write-Warning 'terminal64.exe is NOT running.' }
if ($History) {
    # NOTE: A terminated wrapper can leave a child running. Report known module
    # processes before suggesting a restart; never kill a trading process here.
    Write-Host "`nPython processes for ICT modules (check for duplicates before restarting):"
    try {
        $Modules = @{
            'brain.app' = 'ICT-Brain'; 'brain.dashboard' = 'ICT-Dashboard'
            'executor.mt5_bridge' = 'ICT-Candles'; 'executor.order_executor' = 'ICT-Executor'
            'executor.trade_reporter' = 'ICT-Equity'; 'executor.watchdog' = 'ICT-Watchdog'
        }
        $Processes = @(foreach ($Process in Get-CimInstance -ClassName Win32_Process `
                -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop) {
            foreach ($Module in $Modules.Keys) {
                if ($Process.CommandLine -match ('(?:^|\s)-m\s+' + [regex]::Escape($Module) + '(?:\s|$)')) {
                    $State = ($Rows | Where-Object { $_.Task -eq $Modules[$Module] }).State
                    [pscustomobject]@{ Task = $Modules[$Module]; TaskState = $State; PID = $Process.ProcessId;
                        ParentPID = $Process.ParentProcessId; Session = $Process.SessionId; Executable = $Process.ExecutablePath }
                }
            }
        })
        if ($Processes.Count -gt 0) {
            $Processes | Format-Table -AutoSize
            if (@($Processes | Where-Object { $_.TaskState -ne 'Running' }).Count -gt 0) {
                Write-Warning 'A matching Python process exists while its task is not Running. Verify whether it is a manual/orphaned instance before starting another copy.'
            }
        } else { Write-Host 'No matching ICT Python command lines found (process visibility may require elevation).' }
    } catch { Write-Warning 'Could not inspect local Python processes; inspect Task Manager before restarting tasks.' }
    Write-Host "`nTask Scheduler history for ICT tasks (last 24 hours):"
    try {
        $Events = @(Get-WinEvent -FilterHashtable @{
            LogName = 'Microsoft-Windows-TaskScheduler/Operational'
            StartTime = (Get-Date).AddHours(-24)
            Id = @(100, 101, 102, 111, 129, 200, 201, 203)
        } -MaxEvents 200 -ErrorAction Stop)
        $HistoryRows = @(foreach ($Event in $Events) {
            $Xml = [xml]$Event.ToXml()
            $TaskField = @($Xml.Event.EventData.Data | Where-Object { $_.Name -eq 'TaskName' })
            if ($TaskField.Count -gt 0 -and ($Names | ForEach-Object { '\' + $_ }) -contains $TaskField[0].InnerText) {
                [pscustomobject]@{ Time = $Event.TimeCreated; Id = $Event.Id; Task = $TaskField[0].InnerText; Detail = $Event.Message }
            }
        })
        if ($HistoryRows.Count -gt 0) { $HistoryRows | Format-List }
        else { Write-Host 'No recent ICT events in the retrieved history.' }
    } catch {
        Write-Warning 'Task history unavailable or empty. Enable it in Task Scheduler > Enable All Tasks History, then reproduce the interruption.'
    }
    Write-Host 'Event 111 indicates task termination; 201 records action completion/result. Correlate their timestamps with Windows session events.'
}
$LogDir = Join-Path $RepoRoot 'logs'
$Logs = @(Get-ChildItem -LiteralPath $LogDir -Filter '*.log' -File -ErrorAction SilentlyContinue)
if (-not $Logs) { Write-Host 'No logs yet.' }
foreach ($Log in $Logs) {
    Write-Host "`n--- $($Log.Name) (last $Tail lines) ---"
    Get-Content -LiteralPath $Log.FullName -Tail $Tail
}
# NOTE: Skip equity-age network queries here. The dashboard reports snapshot
# age and stale data; status.ps1 stays usable when the database is unavailable.

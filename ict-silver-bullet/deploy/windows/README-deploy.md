# Deploy on the always-on EC2 Windows instance

Run these commands **on EC2**, in Windows PowerShell 5.1, as the same Windows
user that runs MT5. Install Git, 64-bit Python 3.10+, and Node.js 24 LTS
(including npm) first, then open a new PowerShell so they are on PATH.
Configure Windows auto-logon for that user before the reboot test. The tasks
run only while that user is logged on; a boot without auto-logon starts nothing.
Install MT5, log into the intended account, enable algorithmic trading, and
verify the Python bridge is allowed. Set `MT5_TERMINAL_PATH` in executor's
environment to the same executable used by `-TerminalPath`.

Disconnect RDP when finished; **never sign out**. Signing out ends the
interactive session required by the MT5 GUI/Python bridge.

Allow TCP 7860 in the EC2 security group **only from your public IP /32**;
scope the Windows firewall rule to the same IP. Dashboard username/password
are required in `brain\.env`. Direct HTTP does not encrypt those credentials;
use a trusted private connection or HTTPS for sensitive access. If you later
use Tailscale (free personal plan), set `DASHBOARD_HOST` to the tailnet IP,
or use `127.0.0.1` with local forwarding, and remove the public 7860 rule.

## First installation

1. RDP into EC2 as the auto-logon user. Open Windows PowerShell.
2. Clone with HTTPS. At Git's credential prompt, enter your username and
   **read-only repository token** as the password. Do not put the token in the
   clone URL, a script, or shell history. A read-only token can pull updates.
3. Create both `.env` files manually using the root `.env.example` as a reference.
   Save only settings relevant to each process. Include `DASHBOARD_USERNAME`
   and `DASHBOARD_PASSWORD` in Brain's file. Never commit either file.
4. Bootstrap, then register tasks. Registration is idempotent and does not
   start trading immediately; the first automatic start is the next logon.
5. Reboot to test auto-logon. Reconnect with RDP as the same user, wait at least
   60 seconds, and inspect tasks, stderr logs, MT5, fresh candles and equity.
6. Open `http://<Elastic IP>:7860`, authenticate, and check the equity snapshot
   age. Disconnect RDP after verifying operation.

The upstream repository currently contains the app in a nested directory:

```powershell
Set-Location C:\
git clone https://github.com/thanmaydr0/ict-silver-bullet.git C:\ict-silver-bullet
Set-Location C:\ict-silver-bullet\ict-silver-bullet
Get-Content .env.example
notepad .\brain\.env
notepad .\executor\.env
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\bootstrap.ps1
.\deploy\windows\install_tasks.ps1 -TerminalPath 'C:\Program Files\MetaTrader 5\terminal64.exe'
Restart-Computer
```

Replace the terminal path with the actual broker installation path. After
the reboot and RDP reconnection:

```powershell
Set-Location C:\ict-silver-bullet\ict-silver-bullet
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\status.ps1 -Health
Start-Process 'http://<Elastic IP>:7860'
```

Use the instance's actual Elastic IP in the browser command. Firewall rules
and auto-logon configuration are manual; these scripts do not contact AWS.

## Task behavior and logs

`ICT-MT5Terminal` starts at logon. Six Python tasks start with a fixed 60-second
logon delay, with the repo as their working directory: Candles, Executor,
Equity, Watchdog, Brain and Dashboard. Each uses its own package's venv.
Interactive logon is used (no stored task password), no execution time limit,
start-when-available, ignore duplicate instances, and a failure restart every
minute up to the documented XML schema maximum of 255 attempts
([Microsoft Count schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-count-restarttype-element)).

Python stdout goes to `logs\<name>.out.log`; raw stderr goes to
`logs\<name>.err.log`. Names are `candles`, `executor`, `equity`, `watchdog`,
`brain`, `dashboard`. Application logs may additionally use shared
`brain.log` / `executor.log` files. The launcher uses `Start-Process` with a
separate hidden console, waits for Python, and returns its exit code to Task
Scheduler. Process-level stream redirection keeps ordinary stderr logging
out of PowerShell's error pipeline. Before each start, any prior output file
is moved to `<name>.<out/err>.log.<UTC timestamp>.<launcher PID>.previous`;
startup errors from earlier runs remain available. Monitor disk use and
archive/remove old `.previous` files periodically after reviewing them.
The terminal itself is a GUI task, so inspect MT5's own Journal for errors.

`status.ps1 -Tail 20` shows task state, last result, terminal PIDs, and all log
tails without requiring a network connection. Add `-Health` for a two-second
localhost HTTP check showing the Vite build ID and API version; use
`-DashboardPort` when overriding 7860. A tailnet-only bind needs a health check
at that configured IP instead. The dashboard displays source ages and stale
warnings, including when the reporter stops publishing but Supabase still answers.

## Console interruption diagnosis and launcher upgrade

`0xC000013A` is Windows `STATUS_CONTROL_C_EXIT`, a console interruption
([Microsoft NTSTATUS values](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-erref/596a1078-e883-4972-9bbc-49e60bebca55)).
It does not, by itself, identify who/what interrupted the process. Check task
history and session events; do not conclude that Python crashed from this
code alone. Older launchers wrapped even INFO logs on stderr in
`NativeCommandError`/`RemoteException`; those labels do not turn an INFO line
into a Python exception. Old application/test log entries are also not
evidence of a current failure: compare their timestamps to the task run.

`status.ps1 -History` includes recent ICT Task Scheduler events and matching
Python module PIDs/session IDs. It warns if a matching Python process remains
while its scheduled task is not Running. Inspect such a manual/orphaned
instance before starting another copy, especially the trading Executor.
This diagnostic never kills processes or starts tasks. If history is disabled,
enable **Task Scheduler > Enable All Tasks History**, reproduce, and rerun
status. Event 111 records task termination; 201 records the action result.
There may be no retained evidence for an interruption before history was enabled.

Pulling Git alone does not change registered task actions. To install the new
launcher while preserving the existing terminal executable path:

```powershell
Set-Location C:\ict-silver-bullet\ict-silver-bullet
Set-ExecutionPolicy -Scope Process Bypass
$TerminalPath = (Get-ScheduledTask -TaskName 'ICT-MT5Terminal').Actions[0].Execute
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) { throw 'Git update failed' }
.\deploy\windows\status.ps1 -History
.\deploy\windows\install_tasks.ps1 -TerminalPath $TerminalPath
```

Registration does not stop or restart currently running tasks. A running task
will use the new launcher on its next start. For the reported case where all
six Python tasks are Ready, after verifying there are no remaining matching
Python processes, MT5 is running and logged into the correct account, start
the stopped tasks with Executor last:

```powershell
foreach ($Name in @('ICT-Candles', 'ICT-Equity', 'ICT-Watchdog', 'ICT-Dashboard', 'ICT-Brain', 'ICT-Executor')) {
    if ((Get-ScheduledTask -TaskName $Name).State -ne 'Running') {
        Start-ScheduledTask -TaskName $Name
    }
}
Start-Sleep -Seconds 15
.\deploy\windows\status.ps1 -History
```

If the interruption recurs while you remain connected and do not stop any
tasks, capture that history output. A separate hidden Python console improves
launcher isolation; interactive tasks still cannot survive Windows sign-out.

## Updates

### First upgrade from Gradio to Vite

Install Node.js 24 LTS and reopen PowerShell first. The old updater cannot build
Vite assets, so fetch the new updater into your temporary directory and run it
against the existing checkout. Fetching does not change the running source.
This performs the full guarded update because Python requirements change:

```powershell
Set-Location C:\ict-silver-bullet\ict-silver-bullet
Set-ExecutionPolicy -Scope Process Bypass
git fetch origin main
if ($LASTEXITCODE -ne 0) { throw 'Git fetch failed' }
$Updater = Join-Path $env:TEMP 'ict-vite-update.ps1'
$UpdaterSource = git show origin/main:ict-silver-bullet/deploy/windows/update.ps1
if ($LASTEXITCODE -ne 0) { throw 'Could not retrieve the new updater' }
Set-Content -LiteralPath $Updater -Value $UpdaterSource -Encoding UTF8
& $Updater -RepositoryRoot (Get-Location).Path
.\deploy\windows\status.ps1 -Health
```

The position guard may refuse this update; close positions before retrying.
Existing credentials, port, task registration and hidden Python launcher stay
the same. `python -m brain.dashboard` now serves FastAPI and the compiled React
app. A permanent Node process is not needed. Missing or corrupt assets refuse
startup with a build instruction in `dashboard.err.log`. Reload the browser
and sign in after the upgrade.

### Subsequent updates

```powershell
Set-Location C:\ict-silver-bullet\ict-silver-bullet
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\update.ps1
.\deploy\windows\status.ps1 -Health
```

Updates require a clean checkout on `main`. The script checks both database
open trades and actual MT5 positions against the configured account. Open
positions, a stale database open-trade row, or an unverifiable check abort
the update. It quiesces Brain and Executor, checks again for a racing entry,
then pulls `origin main` and reinstalls a venv's requirements only when its
requirements changed (including Brain ML and backtest dependencies). A failed
dependency install leaves a pending marker so the next retry repairs it even
when Git already contains the new requirements.
Frontend input hashes include source, package lock, TypeScript/Vite settings
and build scripts. Changed inputs or a pending build run `npm.cmd ci` and build
to `frontend\dist.next`. The manifest and referenced assets are validated
before promotion; failures retain the current `dist` and the pending marker.
On success it starts data/reporting services before Brain, with Executor last.
A running MT5 terminal is retained; an absent terminal is started and given
60 seconds to initialize. Manual task starts do not use the logon delay.

If an update fails after stopping tasks, they remain stopped for operator
review. Resolve Git/dependency errors and check positions before retrying.
Never blindly restart Executor after a failed safety check. Stopping tasks
does not close broker positions. The trade manager cannot run while stopped.

Only if you consciously accept interruption of management for open/unknown
positions, use the explicit override:

```powershell
.\deploy\windows\update.ps1 -Force
```

For later changes confined to dashboard code, frontend or its deployment/docs:

```powershell
.\deploy\windows\update.ps1 -DashboardOnly
.\deploy\windows\status.ps1 -Health
```

The updater checks the Python processes as well as Task Scheduler state. Stopping
a scheduled PowerShell wrapper can leave its hidden Python child alive, including
the Windows venv redirector/base-interpreter pair. The updater captures those
processes before stopping each wrapper and terminates surviving instances only
when their module and checkout-specific venv identity match. It checks process
creation times before termination to avoid acting on a reused PID. An inaccessible
process identity or a child that cannot be stopped aborts the update before Git
or dependency changes. Other checkout processes and `terminal64.exe` are preserved.

For a full update the position guard runs before stopping anything, again after
Brain is stopped but before stopping Executor, and after all six services stop.
If an entry races the first check, Executor remains running for operator review.
Each restart must show both a Running task and its Python process for at least
two seconds; a startup failure stops the sequence before starting subsequent
services. Earlier services may already be running. Use `status.ps1 -History -Health`
after any failure and inspect positions before retrying. These startup checks
verify process supervision; the health check and fresh data verify application
readiness.

This inspects incoming paths and refuses shared Python dependency, trading,
configuration or other changes before stopping anything. It stops only
ICT-Dashboard, fast-forwards, builds and restarts only ICT-Dashboard. Trading
tasks continue. A pending Python install requires the full update. If the build
fails, fix the error and rerun; the prior bundle remains, but the dashboard
stays stopped because its Python code has advanced. The flag does not bypass
the position guard for a full update.

### Frontend recovery

`dist.previous` holds the previous production bundle. Earlier hashed assets
are retained in each promoted build for already-open browser tabs. Review disk
use periodically; clean old assets only with the dashboard stopped and clients
reloaded. Do not serve a previous frontend against an incompatible API version.
The API contract here remains version 1.

To restore the previous build after reviewing API compatibility, with the
dashboard stopped (and from the app directory):

```powershell
Stop-ScheduledTask -TaskName 'ICT-Dashboard'
if ((Get-ScheduledTask -TaskName 'ICT-Dashboard').State -eq 'Running') { throw 'Wait for dashboard to stop' }
$DashboardChildren = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
    Where-Object { $_.CommandLine -match '(?:^|\s)-m\s+brain\.dashboard(?:\s|$)' })
if ($DashboardChildren.Count -gt 0) { throw 'Dashboard Python still running; use the updater to recover before moving bundles.' }
if (-not (Test-Path -LiteralPath '.\frontend\dist.previous\build.json')) { throw 'No previous build' }
if (Test-Path -LiteralPath '.\frontend\dist.failed') { throw 'Move or review the existing dist.failed first' }
Move-Item -LiteralPath '.\frontend\dist' -Destination '.\frontend\dist.failed'
Move-Item -LiteralPath '.\frontend\dist.previous' -Destination '.\frontend\dist'
Start-ScheduledTask -TaskName 'ICT-Dashboard'
.\deploy\windows\status.ps1 -Health
```

For a missing initial build, run `build_frontend.ps1` while ICT-Dashboard is
stopped, then start that task. Rebuilding the current source is the preferred
recovery. A frontend rollback does not roll back Python dependencies or Git.

### Sessions and HTTPS

All data endpoints require a cookie session; `/healthz` returns only a build ID,
status and schema version. Login/logout require an exact same-origin browser
request. Sessions expire after eight hours, logout revokes them, and restarting
the single Python worker logs everyone out. Ten login attempts per IP per five
minutes are allowed. No Supabase key or session token enters local storage.

For HTTPS terminated at a trusted reverse proxy, manually add these optional
settings to `brain\.env` (process environment overrides them):

```text
DASHBOARD_COOKIE_SECURE=true
DASHBOARD_TRUSTED_PROXY_IP=127.0.0.1
```

Use the actual proxy IP and restrict backend access to it. Only that explicit IP
may supply forwarded protocol/client headers; wildcards are rejected. Preserve
the original Host and Origin. Bind the backend to loopback for a proxy on the
same instance. Plain HTTP mode remains available for the existing private
connection; use encrypted access before sending credentials over public networks.
Local Vite development uses HTTP and requires secure-cookie mode off.

To remove task registration (including stopping tasks):

```powershell
.\deploy\windows\install_tasks.ps1 -Uninstall
```

## Data needed from other phases

The current schema exposes equity, joined trade fields and news. It does not
persist per-trade floating P/L or detector evidence. Open positions therefore
show `Unavailable (not reported)` until the reporter/schema supplies
`floating_pnl` in account currency. Do not confuse this with a zero P/L.

For a faithful setup audit, persist a signal `setup` JSON object with the
original `candles` window (UTC `ts`, open/high/low/close), `fvg` (top/bottom),
`ob` (high/low), `mss` (level) and `sweep` (level or wick_extreme). The dashboard
accepts a dictionary or list of dictionaries for each detector field. Without this evidence it queries
up to 100 pre-signal M1 candles and explicitly marks missing overlays. It never
uses candles after detection or recomputes detector evidence.

The equity curve shows 24 hours, caches history for one minute, and refreshes
the latest point/gauges every 10 seconds. Large histories retain bucket
equity/reference-line extrema and drawdown peaks for display; reference lines follow the persisted
daily and overall baselines rather than assuming a fixed balance.

The ±15-minute news blackout requires a read of events in the preceding
15 minutes as well as `get_upcoming_news`; this dashboard reads the complete
±15-minute gate using the existing
Supabase SDK in a strict read adapter with five-second request timeouts, without
changing existing DB interfaces. History pagination is bounded to 50,000 rows
and a deadline, with overflow/errors reported as stale or unavailable. If the
news read fails or its data ages beyond 30 seconds, blackout is unknown. The killzone label indicates window timing;
it does not certify the pipeline is running or all risk guards permit entries.

Production scripts have only been parsed and tested with mocks here; perform
the reboot/MT5 validation on a demo account before enabling real trading.

# Deploy on the always-on EC2 Windows instance

Run these commands **on EC2**, in Windows PowerShell 5.1, as the same Windows
user that runs MT5. Install Git and 64-bit Python 3.10+ first (on PATH).
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
.\deploy\windows\status.ps1
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

Python stdout appends to `logs\<name>.out.log`; stderr appends to
`logs\<name>.err.log`. Names are `candles`, `executor`, `equity`, `watchdog`,
`brain`, `dashboard`. Application logs may additionally use shared
`brain.log` / `executor.log` files. In PowerShell 5.1 native stderr can contain
`NativeCommandError` formatting; the original startup error is retained.
Monitor disk use and archive the append-only task output logs periodically.
The terminal itself is a GUI task, so inspect MT5's own Journal for errors.

`status.ps1 -Tail 20` shows task state, last result, terminal PIDs, and all log
tails without requiring a network connection. The dashboard displays equity
snapshot age and a stale warning after 30 seconds.

## Updates

```powershell
Set-Location C:\ict-silver-bullet\ict-silver-bullet
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\update.ps1
.\deploy\windows\status.ps1
```

Updates require a clean checkout on `main`. The script checks both database
open trades and actual MT5 positions against the configured account. Open
positions, a stale database open-trade row, or an unverifiable check abort
the update. It quiesces Brain and Executor, checks again for a racing entry,
then pulls `origin main` and reinstalls a venv's requirements only when its
requirements changed (including Brain ML and backtest dependencies). A failed
dependency install leaves a pending marker so the next retry repairs it even
when Git already contains the new requirements.
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
accepts a dict or list for each detector field. Without this evidence it shows
available pre-signal M1 candle context and explicitly marks missing overlays.
An old signal outside the latest 500 candles can have no available window.

The equity curve shows 24 hours, caches history for one minute, and refreshes
the latest point/gauges every 10 seconds. Large histories retain bucket
extrema and drawdown peaks for display; reference lines follow the persisted
daily and overall baselines rather than assuming a fixed balance.

The ±15-minute news blackout requires a read of events in the preceding
15 minutes as well as `get_upcoming_news`; this dashboard reads the complete
±15-minute gate using the existing
Supabase client's read machinery without changing its interface. If the recent
read fails, blackout is unknown. The killzone label indicates window timing;
it does not certify the pipeline is running or all risk guards permit entries.

Production scripts have only been parsed and tested with mocks here; perform
the reboot/MT5 validation on a demo account before enabling real trading.

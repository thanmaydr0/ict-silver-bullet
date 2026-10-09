"""Exercise deployment registration with mocked Scheduler cmdlets, never MT5/AWS."""

import base64
import json
import re
from pathlib import Path
import shutil
import subprocess
import sys
import venv

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "windows"
PS = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(not PS, reason="Windows PowerShell 5.1 required")


def ps(code):
    # Load exported utility functions in the top-level harness scope before
    # mock functions trigger autoload inside a transient function scope.
    code = "Import-Module Microsoft.PowerShell.Utility; " + code
    result = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-Command", code],
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def test_all_scripts_parse_in_windows_powershell():
    ps("$ErrorActionPreference='Stop'; "
       f"Get-ChildItem -LiteralPath {quote(SCRIPTS)} -Filter '*.ps1' | ForEach-Object {{ "
       "$tokens=$null; $errors=$null; "
       "[System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$errors) | Out-Null; "
       "if ($errors) { throw ($errors | Out-String) } }")


@pytest.mark.parametrize("native_exit", [0, 2, 3])
def test_position_check_transport_in_real_powershell_and_python(tmp_path, native_exit):
    source = (SCRIPTS / "update.ps1").read_text(encoding="utf-8")
    function = "function Assert-NoOpenPositions {" + source.split(
        "function Assert-NoOpenPositions {", 1)[1].split("Push-Location", 1)[0]
    check = re.search(r"\$Check = @'\n(.*?)\n'@", function, re.S)
    assert check
    original_guard = base64.b64encode(check.group(1).encode("utf-8")).decode("ascii")
    expected = {"message": 'missing "database" response', "path": r"C:\Program Files\fixture", "unicode": "Caf\u00e9"}
    # Compile the complete production guard without executing its imports. Only
    # this harmless payload executes: no MT5, SDK, configuration or service calls.
    payload = ("import base64, json, sys\n"
               f'compile(base64.b64decode("{original_guard}"), "<guard-syntax>", "exec")\n'
               f'payload = {json.dumps(expected, ensure_ascii=False)}\n'
               'print("PAYLOAD:" + json.dumps(payload))\n'
               f"sys.exit({native_exit})\n")
    function = function[:check.start(1)] + payload.rstrip("\n") + function[check.end(1):]
    runtime = tmp_path / "Python runtime with spaces"
    venv.EnvBuilder(with_pip=False).create(runtime)
    python = runtime / "Scripts" / "python.exe"
    output = ps("$ErrorActionPreference='Stop'; " + function +
                f"\n$ExecutorPython={quote(python)}; $Caught=$null; "
                "try { Assert-NoOpenPositions } catch { $Caught=$_.Exception.Message }; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{Exit=$LASTEXITCODE;Caught=$Caught}))")
    assert json.loads(output.split("PAYLOAD:", 1)[1].splitlines()[0]) == expected
    result = json.loads(output.split("RESULT:", 1)[1])
    assert result["Exit"] == native_exit
    if native_exit == 0:
        assert result["Caught"] is None
    else:
        assert "Update aborted" in result["Caught"]
        assert ("Open positions exist." if native_exit == 2 else "Position check failed") in output


def test_bootstrap_python_version_check_uses_native_python():
    source = (SCRIPTS / "bootstrap.ps1").read_text(encoding="utf-8")
    command = next(line.strip() for line in source.splitlines() if "& $Python -c" in line)
    output = ps(f"$Python={quote(sys.executable)}; " + command +
                "; Write-Output ('RESULT:' + $LASTEXITCODE)")
    assert output.split("RESULT:")[-1].strip() == "0"


def test_frontend_version_and_hash_checks_use_real_node_without_installing(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for native argument regression")
    root = tmp_path / "repo with spaces"
    scripts = root / "deploy" / "windows"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPTS / "build_frontend.ps1", scripts)
    frontend = root / "frontend"
    (frontend / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPTS.parents[1] / "frontend" / "scripts" / "write-build.mjs", frontend / "scripts")
    (frontend / "index.html").write_text("source fixture", encoding="utf-8")
    hashed = subprocess.run([node, "scripts/write-build.mjs", "--source-hash"], cwd=frontend,
                            capture_output=True, text=True, timeout=15, check=True).stdout
    current = frontend / "dist"
    (current / "assets").mkdir(parents=True)
    (current / "assets" / "fixture.js").write_text("fixture", encoding="utf-8")
    (current / "index.html").write_text('<script src="/assets/fixture.js"></script>', encoding="utf-8")
    (current / "build.json").write_text(json.dumps({"schema_version":1,"build_id":"fixture","source_hash":hashed}),encoding="utf-8")
    output = ps("function npm.cmd { throw 'Test must not install packages or call the network' }; "
                f"& {quote(scripts / 'build_frontend.ps1')} -StageOnly")
    assert "Frontend unchanged: fixture" in output


@pytest.fixture
def registration(tmp_path):
    target = tmp_path / "repo with spaces" / "deploy" / "windows"
    target.mkdir(parents=True)
    shutil.copy(SCRIPTS / "install_tasks.ps1", target)
    root = target.parents[1]
    for package in ("brain", "executor"):
        exe = root / package / ".venv" / "Scripts" / "python.exe"
        exe.parent.mkdir(parents=True)
        exe.write_text("not executed", encoding="utf-8")
    terminal = root / "terminal64.exe"
    terminal.write_text("not executed", encoding="utf-8")
    # .env existence is mocked rather than creating credential files.
    harness = r"""
$ErrorActionPreference='Stop'
$global:Registered = @{}
$global:Stopped = @()
function Test-Path { param($LiteralPath, $PathType)
    if ($LiteralPath -like '*.env') { return $true }
    return Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath
}
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel)
    return [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel}
}
function New-ScheduledTaskSettingsSet {
    param($ExecutionTimeLimit,$RestartInterval,$RestartCount,$MultipleInstances,
        [switch]$StartWhenAvailable,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries)
    return [pscustomobject]@{Limit=$ExecutionTimeLimit.TotalSeconds;RestartSeconds=$RestartInterval.TotalSeconds;
        RestartCount=$RestartCount;StartWhenAvailable=[bool]$StartWhenAvailable;Instances=$MultipleInstances}
}
function New-ScheduledTaskTrigger { param([switch]$AtLogOn,$User)
    return [pscustomobject]@{AtLogOn=[bool]$AtLogOn;User=$User;Delay=$null}
}
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory)
    return [pscustomobject]@{Execute=$Execute;Argument=$Argument;WorkingDirectory=$WorkingDirectory}
}
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Settings,$Principal,$Description,[switch]$Force)
    $global:Registered[$TaskName]=[pscustomobject]@{Name=$TaskName;Action=$Action;Trigger=$Trigger;Settings=$Settings;Principal=$Principal}
}
function Get-ScheduledTask { param($TaskName,$ErrorAction)
    return $global:Registered[$TaskName]
}
function Stop-ScheduledTask { param($TaskName)
    $global:Stopped += $TaskName
}
function Unregister-ScheduledTask { param($TaskName,$Confirm)
    $global:Registered.Remove($TaskName)
}
"""
    return root, target / "install_tasks.ps1", terminal, harness


def test_install_is_idempotent_and_uses_interactive_logon(registration):
    root, script, terminal, harness = registration
    invoke = f"& {quote(script)} -TerminalPath {quote(terminal)} | Out-Null; "
    output = ps(harness + invoke + invoke +
                "Write-Output ('RESULT:' + (ConvertTo-Json -Depth 8 -Compress -InputObject @($global:Registered.Values)))")
    tasks = json.loads(output.split("RESULT:")[-1])
    assert len(tasks) == 7
    for task in tasks:
        assert task["Principal"]["LogonType"] == "Interactive"
        assert task["Trigger"]["AtLogOn"] is True
        assert task["Settings"]["Limit"] == 0
        assert task["Settings"]["RestartSeconds"] == 60
        assert task["Settings"]["RestartCount"] == 255
        assert task["Settings"]["StartWhenAvailable"] is True
        assert task["Action"]["WorkingDirectory"] == str(root)
        if task["Name"] == "ICT-MT5Terminal":
            assert task["Trigger"]["Delay"] is None
            assert task["Action"]["Execute"] == str(terminal)
        else:
            assert task["Trigger"]["Delay"] == "PT60S"
            encoded = task["Action"]["Argument"].split()[-1]
            command = base64.b64decode(encoded).decode("utf-16-le")
            package = "brain" if task["Name"] in ("ICT-Brain", "ICT-Dashboard") else "executor"
            assert f"{package}\\.venv\\Scripts\\python.exe" in command
            assert "-RedirectStandardError $Stderr" in command and ".err.log" in command
            assert "-WindowStyle Hidden" in command
            assert "-Wait -PassThru" in command
            assert "exit $Child.ExitCode" in command


def test_uninstall_stops_and_removes_all_tasks(registration):
    _, script, terminal, harness = registration
    output = ps(harness + f"& {quote(script)} -TerminalPath {quote(terminal)} | Out-Null; "
                f"& {quote(script)} -Uninstall | Out-Null; & {quote(script)} -Uninstall | Out-Null; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{Remaining=$global:Registered.Count;Stopped=$global:Stopped}))")
    result = json.loads(output.split("RESULT:")[-1])
    assert result["Remaining"] == 0
    assert len(result["Stopped"]) == 7
    assert result["Stopped"][0:2] == ["ICT-Brain", "ICT-Executor"]


@pytest.mark.parametrize("exit_code,line", [(7, "startup failure"), (0, "INFO | outside_killzone")])
def test_python_wrapper_preserves_stderr_and_exit_code(registration, exit_code, line):
    root, script, terminal, harness = registration
    output = ps(harness + f"& {quote(script)} -TerminalPath {quote(terminal)} | Out-Null; "
                "Write-Output ('RESULT:' + $global:Registered['ICT-Executor'].Action.Argument)")
    command = base64.b64decode(output.split("RESULT:")[-1].split()[-1]).decode("utf-16-le")
    command = command.replace(str(root / "executor" / ".venv" / "Scripts" / "python.exe"), sys.executable)
    command = command.replace("executor.order_executor", "mock_entry")
    (root / "mock_entry.py").write_text(f"import sys\nprint({line!r}, file=sys.stderr)\nsys.exit({exit_code})\n", encoding="utf-8")
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, timeout=30)
    assert result.returncode == exit_code
    stderr = (root / "logs" / "executor.err.log").read_text(encoding="utf-8-sig")
    assert line in stderr
    assert "NativeCommandError" not in stderr
    assert "RemoteException" not in stderr
    # A second run must archive, not erase, the previous startup failure.
    result = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, timeout=30)
    assert result.returncode == exit_code
    previous = list((root / "logs").glob("executor.err.log.*.previous"))
    assert len(previous) == 1
    assert line in previous[0].read_text(encoding="utf-8-sig")


def test_launcher_records_failure_to_start_python(registration):
    root, script, terminal, harness = registration
    output = ps(harness + f"& {quote(script)} -TerminalPath {quote(terminal)} | Out-Null; "
                "Write-Output ('RESULT:' + $global:Registered['ICT-Dashboard'].Action.Argument)")
    command = base64.b64decode(output.split("RESULT:")[-1].split()[-1]).decode("utf-16-le")
    command = command.replace(str(root / "brain" / ".venv" / "Scripts" / "python.exe"), str(root / "missing.exe"))
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, timeout=30)
    assert result.returncode == 1
    assert "Launcher failure:" in (root / "logs" / "dashboard.err.log").read_text(encoding="utf-8-sig")


def test_status_reports_interruption_and_filters_history(tmp_path):
    source = quote(SCRIPTS / "status.ps1")
    harness = r"""
function Get-ScheduledTask { param($TaskName,$ErrorAction)
    return [pscustomobject]@{State='Ready'}
}
function Get-ScheduledTaskInfo { param($TaskName)
    return [pscustomobject]@{LastRunTime=[datetime]'2026-10-08T06:19:13';LastTaskResult=[uint32]3221225786}
}
function Get-Process { param($Name,$ErrorAction) }
function Get-ChildItem { param($LiteralPath,$Filter,[switch]$File,$ErrorAction) }
function Get-CimInstance { param($ClassName,$Filter,$ErrorAction)
    return [pscustomobject]@{CommandLine='python.exe -u -m brain.app';ProcessId=1234;ParentProcessId=1000;
        SessionId=1;ExecutablePath='C:\Python310\python.exe'}
}
function Get-WinEvent { param($FilterHashtable,$MaxEvents,$ErrorAction)
    $First = [pscustomobject]@{TimeCreated=[datetime]'2026-10-08T06:19:39';Id=111;Message='ICT task was terminated'}
    $First | Add-Member -MemberType ScriptMethod -Name ToXml -Value { '<Event><EventData><Data Name="TaskName">\ICT-Brain</Data></EventData></Event>' }
    $Other = [pscustomobject]@{TimeCreated=[datetime]'2026-10-08T06:19:40';Id=111;Message='UNRELATED TASK'}
    $Other | Add-Member -MemberType ScriptMethod -Name ToXml -Value { '<Event><EventData><Data Name="TaskName">\OtherApp</Data></EventData></Event>' }
    return @($First,$Other)
}
"""
    output = ps(harness + f"& {source} -History")
    assert "0xC000013A" in output
    assert "console interruption" in output
    assert "ICT task was terminated" in output
    assert "UNRELATED TASK" not in output
    assert "manual/orphaned instance" in " ".join(output.split()), output
    assert "1234" in output


def test_status_survives_unavailable_task_history():
    harness = r"""
function Get-ScheduledTask { param($TaskName,$ErrorAction) }
function Get-Process { param($Name,$ErrorAction) }
function Get-ChildItem { param($LiteralPath,$Filter,[switch]$File,$ErrorAction) }
function Get-CimInstance { param($ClassName,$Filter,$ErrorAction) }
function Get-WinEvent { param($FilterHashtable,$MaxEvents,$ErrorAction) throw 'History disabled' }
"""
    output = ps(harness + f"& {quote(SCRIPTS / 'status.ps1')} -History")
    assert "Task history unavailable or empty" in output


@pytest.fixture
def update_harness(tmp_path):
    root = tmp_path / "repo"
    target = root / "deploy" / "windows"
    target.mkdir(parents=True)
    # Inject a PS shim as the venv command. Production control flow stays intact;
    # no real Python/pip/MT5/database command runs in this fixture.
    source = (SCRIPTS / "update.ps1").read_text(encoding="utf-8")
    script = target / "update.ps1"
    script.write_text(source.replace("Scripts\\python.exe", "Scripts\\python.ps1"), encoding="utf-8")
    (target / "build_frontend.ps1").write_text(
        "$global:BuildCalls++; if ($global:FailBuild) { throw 'Mock build failed' }", encoding="utf-8")
    for package in ("brain", "executor"):
        shim = root / package / ".venv" / "Scripts" / "python.ps1"
        shim.parent.mkdir(parents=True)
        shim.write_text("if ($args -contains '-c') { $global:GuardCalls++; "
                        "$global:LASTEXITCODE = if ($global:GuardCalls -gt 1 -and $null -ne $global:GuardNext) { $global:GuardNext } else { $global:GuardResult } } "
                        "else { $global:PipCalls += $MyInvocation.MyCommand.Path; $global:LASTEXITCODE = $global:PipResult }",
                        encoding="utf-8")
    files = ["brain/requirements.txt", "brain/requirements-ml.txt", "backtest/requirements.txt", "executor/requirements.txt"]
    for name in files:
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("before", encoding="utf-8")
    harness = r"""
$ErrorActionPreference='Stop'
$global:GuardResult=0
$global:GuardCalls=0
$global:GuardNext=$null
$global:PipResult=0
$global:PipCalls=@()
$global:Stopped=@()
$global:Started=@()
$global:Pulls=0
$global:ChangeRequirements=$false
$global:BuildCalls=0
$global:FailBuild=$false
$global:ChangedFiles=@()
function git {
    $global:LASTEXITCODE=0
    if ($args[0] -eq 'branch') { return 'main' }
    if ($args[0] -eq 'status') { return }
    if ($args[0] -eq 'diff') { return $global:ChangedFiles }
    if ($args[0] -eq 'rev-parse') { return 'ict-silver-bullet/' }
    if ($args[0] -eq 'pull') {
        $global:Pulls++
        if ($global:ChangeRequirements) {
            Set-Content -LiteralPath 'brain\requirements.txt' -Value 'after'
            Set-Content -LiteralPath 'executor\requirements.txt' -Value 'after'
            $global:ChangeRequirements=$false
        }
    }
}
function Get-ScheduledTask { param($TaskName,$ErrorAction)
    return [pscustomobject]@{State='Ready';TaskName=$TaskName}
}
function Stop-ScheduledTask { param($TaskName)
    $global:Stopped += $TaskName
}
function Start-ScheduledTask { param($TaskName)
    $global:Started += $TaskName
}
function Get-Process { param($Name,$ErrorAction)
    return [pscustomobject]@{Id=123;Name='terminal64'}
}
"""
    return root, script, harness


def test_update_open_or_unknown_positions_aborts_before_stopping(update_harness):
    _, script, harness = update_harness
    for guard in (2, 3):
        output = ps(harness + f"$global:GuardResult={guard}; "
                    f"try {{ & {quote(script)} | Out-Null; throw 'unexpected success' }} catch {{ $Caught=$_.Exception.Message }}; "
                    "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{Stopped=$global:Stopped.Count;Started=$global:Started.Count;Pulls=$global:Pulls;Caught=$Caught;GuardCalls=$global:GuardCalls}))")
        result = json.loads(output.split("RESULT:")[-1])
        assert result["Stopped"] == result["Started"] == result["Pulls"] == 0
        assert result["GuardCalls"] == 1
        assert "Update aborted" in result["Caught"]


def test_update_skips_unchanged_requirements_and_starts_executor_last(update_harness):
    _, script, harness = update_harness
    output = ps(harness + f"& {quote(script)} | Out-Null; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{PipCalls=$global:PipCalls.Count;Started=$global:Started;Stopped=$global:Stopped;Pulls=$global:Pulls}))")
    result = json.loads(output.split("RESULT:")[-1])
    assert result["PipCalls"] == 0
    assert result["Pulls"] == 1
    assert result["Stopped"][:2] == ["ICT-Brain", "ICT-Executor"]
    assert result["Started"][-1] == "ICT-Executor"


def test_update_retries_both_changed_venvs_after_failed_install(update_harness):
    root, script, harness = update_harness
    output = ps(harness + "$global:ChangeRequirements=$true; $global:PipResult=1; "
                f"try {{ & {quote(script)} | Out-Null }} catch {{ }}; "
                "$global:PipResult=0; $global:PipCalls=@(); "
                f"& {quote(script)} | Out-Null; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{PipCalls=$global:PipCalls;Started=$global:Started}))")
    result = json.loads(output.split("RESULT:")[-1])
    assert len(result["PipCalls"]) == 2
    assert result["Started"][-1] == "ICT-Executor"
    assert not list((root / "logs").glob("*.requirements-pending"))


def test_update_racing_entry_leaves_tasks_stopped(update_harness):
    _, script, harness = update_harness
    output = ps(harness + "$global:GuardNext=2; "
                f"try {{ & {quote(script)} | Out-Null }} catch {{ $Caught=$_.Exception.Message }}; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{Stopped=$global:Stopped.Count;Started=$global:Started.Count;Pulls=$global:Pulls;Caught=$Caught;GuardCalls=$global:GuardCalls}))")
    result = json.loads(output.split("RESULT:")[-1])
    assert result["GuardCalls"] == 2
    assert result["Stopped"] == 6
    assert result["Started"] == result["Pulls"] == 0
    assert "Update aborted" in result["Caught"]


def test_update_force_is_an_explicit_guard_override(update_harness):
    _, script, harness = update_harness
    output = ps(harness + "$global:GuardResult=2; "
                f"& {quote(script)} -Force | Out-Null; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{GuardCalls=$global:GuardCalls;Started=$global:Started}))")
    result = json.loads(output.split("RESULT:")[-1])
    assert result["GuardCalls"] == 0
    assert result["Started"][-1] == "ICT-Executor"


def test_frontend_failure_leaves_full_update_stopped(update_harness):
    _, script, harness = update_harness
    output = ps(harness + "$global:FailBuild=$true; "
                f"try {{ & {quote(script)} | Out-Null }} catch {{ $Caught=$_.Exception.Message }}; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{Builds=$global:BuildCalls;Started=$global:Started.Count;Stopped=$global:Stopped.Count;Caught=$Caught}))")
    result = json.loads(output.split("RESULT:")[-1])
    assert result["Builds"] == 1 and result["Stopped"] == 6 and result["Started"] == 0
    assert "build failed" in result["Caught"]


def test_downloaded_updater_uses_explicit_app_root(update_harness,tmp_path):
    root,script,harness=update_harness
    downloaded=tmp_path/"downloaded-updater.ps1"
    shutil.copy(script,downloaded)
    output=ps(harness + f"& {quote(downloaded)} -RepositoryRoot {quote(root)} | Out-Null; "
              "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{Builds=$global:BuildCalls;Started=$global:Started}))")
    result=json.loads(output.split("RESULT:")[-1])
    assert result["Builds"] == 1 and result["Started"][-1] == "ICT-Executor"


def test_dashboard_only_never_restarts_trading_tasks(update_harness):
    _, script, harness = update_harness
    output = ps(harness + "$global:ChangedFiles=@('ict-silver-bullet/frontend/src/App.tsx'); "
                f"& {quote(script)} -DashboardOnly | Out-Null; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{Builds=$global:BuildCalls;Started=$global:Started;Stopped=$global:Stopped;GuardCalls=$global:GuardCalls}))")
    result = json.loads(output.split("RESULT:")[-1])
    assert result["Started"] == result["Stopped"] == ["ICT-Dashboard"]
    assert result["Builds"] == 1 and result["GuardCalls"] == 0


@pytest.mark.parametrize("changed", ["brain/requirements.txt", "executor/order_executor.py", "brain/config.py"])
def test_dashboard_only_refuses_shared_changes(update_harness, changed):
    _, script, harness = update_harness
    output = ps(harness + f"$global:ChangedFiles=@('ict-silver-bullet/{changed}'); "
                f"try {{ & {quote(script)} -DashboardOnly | Out-Null }} catch {{ $Caught=$_.Exception.Message }}; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{Started=$global:Started.Count;Stopped=$global:Stopped.Count;Caught=$Caught}))")
    result = json.loads(output.split("RESULT:")[-1])
    assert result["Started"] == result["Stopped"] == 0
    assert "full guarded update" in result["Caught"]


def test_build_retries_and_preserves_current_and_hashed_assets(tmp_path):
    root = tmp_path / "repo with spaces"
    target = root / "deploy" / "windows"
    target.mkdir(parents=True)
    script = target / "build_frontend.ps1"
    shutil.copy(SCRIPTS / "build_frontend.ps1", script)
    current = root / "frontend" / "dist"
    (current / "assets").mkdir(parents=True)
    (current / "assets" / "old-hash.js").write_text("old bundle", encoding="utf-8")
    (current / "index.html").write_text('<script src="/assets/old-hash.js"></script>', encoding="utf-8")
    (current / "build.json").write_text(json.dumps({"schema_version":1,"build_id":"old","source_hash":"old-source"}), encoding="utf-8")
    harness = r"""
$global:FailBuild=$true
$global:NpmCi=0
function node { $global:LASTEXITCODE=0; if ($args -contains '--source-hash') { return 'new-source' }; if ($args -contains '--version') { return 'v24.0.0' } }
function npm.cmd {
    $global:LASTEXITCODE=0
    if ($args[0] -eq 'ci') { $global:NpmCi++; return }
    if ($global:FailBuild) { $global:LASTEXITCODE=1; return }
    New-Item -ItemType Directory -Path 'dist.next\assets' -Force | Out-Null
    Set-Content -LiteralPath 'dist.next\assets\new-hash.js' -Value 'new bundle'
    Set-Content -LiteralPath 'dist.next\index.html' -Value '<script src="/assets/new-hash.js"></script>'
    Set-Content -LiteralPath 'dist.next\build.json' -Value '{"schema_version":1,"build_id":"new","source_hash":"new-source"}'
}
function Get-ScheduledTask { param($TaskName,$ErrorAction) return [pscustomobject]@{State='Ready'} }
"""
    output = ps(harness + f"try {{ & {quote(script)} | Out-Null }} catch {{ }}; "
                f"$Preserved=Get-Content -LiteralPath {quote(current / 'build.json')} -Raw | ConvertFrom-Json; "
                f"$Pending=Test-Path -LiteralPath {quote(root / 'logs/frontend.build-pending')}; "
                "$global:FailBuild=$false; " + f"& {quote(script)} | Out-Null; & {quote(script)} | Out-Null; "
                "Write-Output ('RESULT:' + (ConvertTo-Json -Compress -InputObject @{Preserved=$Preserved.build_id;Pending=$Pending;NpmCi=$global:NpmCi}))")
    result = json.loads(output.split("RESULT:")[-1])
    assert result == {"Preserved":"old", "Pending":True, "NpmCi":2}
    assert (current / "assets" / "old-hash.js").exists()
    assert (current / "assets" / "new-hash.js").exists()
    assert (root / "frontend" / "dist.previous" / "index.html").exists()
    assert not (root / "logs" / "frontend.build-pending").exists()

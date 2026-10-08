"""Exercise deployment registration with mocked Scheduler cmdlets, never MT5/AWS."""

import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys

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
function git {
    $global:LASTEXITCODE=0
    if ($args[0] -eq 'branch') { return 'main' }
    if ($args[0] -eq 'status') { return }
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

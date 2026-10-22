<#
Registers the "Naukri Agent - Phone Guard" scheduled task, and stops Windows from ending the phone listener's task
when the network blips.

Run once, in PowerShell, as the signed-in user (no administrator rights needed):

    powershell -ExecutionPolicy Bypass -File scripts\register_phone_guard.ps1

What it does
  1. "Naukri Agent - Phone Guard": every 10 minutes from 06:00 to 23:00 runs `naukri-agent phone-guard`, which starts the
     /apply listener again if Windows ended it, and once a day (10:50) sends the "PC is alive" Telegram message.
     It runs with pythonw.exe so no console window flashes up every 10 minutes. It does not wake the PC; a sleeping PC is
     woken by the morning tasks, which also revive the listener.
  2. "Naukri Agent - Phone Control": switches off "run only if a network connection is available". With it on, a Wi-Fi blip
     (or the PC sleeping) made Task Scheduler stop the listener (result 0x8007042B), and nothing restarted it.

Safe to run again: the guard task is replaced, the other change is idempotent. To undo:
    Unregister-ScheduledTask -TaskName "Naukri Agent - Phone Guard" -Confirm:$false
and tick "Start only if the following network connection is available" again on the Phone Control task if you want it back.
#>
param([string]$Root = (Resolve-Path "$PSScriptRoot\..").Path)

$ErrorActionPreference = "Stop"
$python = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) { throw "pythonw.exe not found at $python - is the virtual environment set up?" }

$action = New-ScheduledTaskAction -Execute $python -WorkingDirectory $Root `
    -Argument '-c "from naukri_agent.cli.main import main; main()" phone-guard'

# daily at 06:00, then repeat every 10 minutes for 17 hours (until 23:00)
$trigger = New-ScheduledTaskTrigger -Daily -At 06:00
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At 06:00 `
    -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Hours 17)).Repetition

# no network condition (the guard must run even when the network is down: it only talks to Windows), never two at once
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "Naukri Agent - Phone Guard" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "naukri-agent phone-guard: restart the /apply listener if Windows ended it; one 'PC is alive' message a day" | Out-Null
Write-Output "Registered 'Naukri Agent - Phone Guard' (every 10 minutes, 06:00-23:00, hidden window)."

$control = Get-ScheduledTask -TaskName "Naukri Agent - Phone Control" -ErrorAction SilentlyContinue
if ($control) {
    $control.Settings.RunOnlyIfNetworkAvailable = $false
    Set-ScheduledTask -TaskName "Naukri Agent - Phone Control" -Settings $control.Settings | Out-Null
    Write-Output "Phone Control: 'run only if a network is available' switched off."
} else {
    Write-Output "Phone Control task not found - nothing changed there."
}

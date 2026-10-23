# Runs the agent in the background at every logon (the Windows equivalent of com.jobagent.plist).
# Run from the repo folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File windows\install-task.ps1
# Remove with:  Unregister-ScheduledTask -TaskName Midna -Confirm:$false
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"   # windowless, so no console stays open
if (-not (Test-Path $Pythonw)) { throw "Missing $Pythonw. Run windows\setup.ps1 first." }

$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "-m job_agent run" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Midna" -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Midna job-finding agent" -Force | Out-Null
Start-ScheduledTask -TaskName "Midna"
Start-Sleep -Seconds 20
Get-ScheduledTask -TaskName "Midna" | Select-Object TaskName, State
Get-Content (Join-Path $Root "logs\agent.log") -Tail 5 -ErrorAction SilentlyContinue

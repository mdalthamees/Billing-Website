$ErrorActionPreference = "Stop"

$python = (Get-Command python -ErrorAction Stop).Source
$script = Join-Path $PSScriptRoot "daily_backup.py"
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$script`"" `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Daily -At 2:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName "HI5 POS Daily MySQL Backup" `
    -Description "Creates a verified local backup of the configured HI-5 MySQL database." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Output "Registered HI5 POS Daily MySQL Backup at 2:00 AM for the current Windows user."

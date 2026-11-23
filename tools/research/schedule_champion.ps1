<# Register once, or invoke one due run with -RunNow. Configuration must be
   refreshed with a new study ID and fresh explicit windows before each cycle.
   Uses Windows Task Scheduler; never stores credentials or changes live trading.
#>
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$OutputRoot,
    [Parameter(Mandatory=$true)][string]$Store,
    [Parameter(Mandatory=$true)][string]$State,
    [string]$TaskName = 'FactorLab-Champion-Research',
    [string]$At = '06:00',
    [switch]$RunNow
)
$ErrorActionPreference = 'Stop'
$scriptPath = $PSCommandPath
$selector = Join-Path $PSScriptRoot 'select_champion.py'
if ($RunNow) {
    & $Python -I $selector --config $Config --output-root $OutputRoot --store $Store --state $State
    exit $LASTEXITCODE
}
foreach ($arg in @($Python, $Config, $OutputRoot, $Store, $State, $scriptPath)) {
    if ($arg.Contains('"') -or $arg.Contains("`n") -or $arg.Contains("`r")) { throw 'Invalid quoted path' }
    if (-not [IO.Path]::IsPathRooted($arg)) { throw 'Scheduled task paths must be absolute' }
}
$arguments = '-I "{0}" --config "{1}" --output-root "{2}" --store "{3}" --state "{4}"' -f $selector,$Config,$OutputRoot,$Store,$State
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
# Register without -Force so an existing task is never silently replaced.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings

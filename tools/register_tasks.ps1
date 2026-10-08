<#
Registers the job-search tasks with Windows Task Scheduler (plan.md Phase 7, plan-tracker.md). From the repo folder:

    powershell -ExecutionPolicy Bypass -File tools\register_tasks.ps1                 # both tasks
    powershell -ExecutionPolicy Bypass -File tools\register_tasks.ps1 -Only tracker   # just one: nightly or tracker

It creates two tasks that run as you, while you're logged in, without a console window:
    jobsearch-nightly   daily at 02:30: every stage, ending with out\<date>\digest.html; its output is in
                        logs\nightly-<date>.txt (Docker Desktop, which runs SearXNG, lives in your session). A run
                        missed while the PC was off starts as soon as it's back on. If a run you started yourself is
                        still going, the nightly waits up to an hour for it.
    jobsearch-tracker   at logon: the tracker page, http://127.0.0.1:8787 (this PC only). No time limit; it's
                        restarted if it stops with an error.
The plan also has the board watcher run on its own every 6 hours. The owner dropped that task on 2026-10-07: only the
nightly writes the report, and it watches the boards itself.

Pause:   Disable-ScheduledTask -TaskName jobsearch-nightly   (or jobsearch-tracker)
Remove:  Unregister-ScheduledTask -TaskName jobsearch-nightly, jobsearch-tracker -Confirm:$false
#>
param([ValidateSet("all", "nightly", "tracker")][string]$Only = "all")
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"  # pythonw: no console window
if (-not (Test-Path $python)) { throw "No virtualenv at $python; see Setup in README.md." }

if ($Only -in "all", "nightly") {
    # The time limit must stay above the nightly's wait for the lock plus the stages' own limits (tests/test_nightly.py
    # checks), so a slow night still gets to the report. The first full run (2026-10-07) took 85 minutes.
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 10)
    $nightly = New-ScheduledTaskAction -Execute $python -Argument "-m jobsearch nightly" -WorkingDirectory $repo
    Register-ScheduledTask -TaskName "jobsearch-nightly" -Action $nightly -Settings $settings `
        -Trigger (New-ScheduledTaskTrigger -Daily -At "02:30") `
        -Description "Job search pipeline: every stage, then the daily report (out\<date>\digest.html)." -Force | Out-Null
}

if ($Only -in "all", "tracker") {
    # No time limit (a zero TimeSpan). If the page stops with an error, it's restarted up to 3 times, a minute apart.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    $tracker = New-ScheduledTaskAction -Execute $python -Argument "-m jobsearch serve" -WorkingDirectory $repo
    Register-ScheduledTask -TaskName "jobsearch-tracker" -Action $tracker -Settings $settings `
        -Trigger (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") `
        -Description "Job search tracker page: http://127.0.0.1:8787 (this PC only)." -Force | Out-Null
}

Get-ScheduledTask -TaskName "jobsearch-*" | Format-Table TaskName, State

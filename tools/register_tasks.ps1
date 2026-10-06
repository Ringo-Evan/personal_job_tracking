<#
Registers the job-search pipeline with Windows Task Scheduler (plan.md Phase 7). Run once, from the repo folder:

    powershell -ExecutionPolicy Bypass -File tools\register_tasks.ps1

It creates two tasks that run as you, while you're logged in (Docker Desktop, which runs SearXNG, lives in your
session):
    jobsearch-nightly   daily at 02:30: every stage, ending with out\<date>\digest.html
    jobsearch-watch     every 6 hours from 08:00: watch the job boards and score new postings
A task missed while the PC was off runs as soon as it's back on. The two share a lock, so they never overlap.
They run without a console window; each run's output is in logs\nightly-<date>.txt.

Pause:   Disable-ScheduledTask -TaskName jobsearch-nightly; Disable-ScheduledTask -TaskName jobsearch-watch
Remove:  Unregister-ScheduledTask -TaskName jobsearch-nightly, jobsearch-watch -Confirm:$false
#>
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\pythonw.exe"  # pythonw: no console window popping up at night
if (-not (Test-Path $python)) { throw "No virtualenv at $python; see Setup in README.md." }

# The time limit must stay above the sum of the stages' own limits (tests/test_nightly.py checks), so a slow night
# still gets to the report. A normal night takes 3-4 hours.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 10)

$nightly = New-ScheduledTaskAction -Execute $python -Argument "-m jobsearch nightly" -WorkingDirectory $repo
Register-ScheduledTask -TaskName "jobsearch-nightly" -Action $nightly -Settings $settings `
    -Trigger (New-ScheduledTaskTrigger -Daily -At "02:30") `
    -Description "Job search pipeline: every stage, then the daily report (out\<date>\digest.html)." -Force | Out-Null

$watch = New-ScheduledTaskAction -Execute $python -Argument "-m jobsearch nightly --watch-only" -WorkingDirectory $repo
$every6h = New-ScheduledTaskTrigger -Once -At "08:00" -RepetitionInterval (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName "jobsearch-watch" -Action $watch -Settings $settings -Trigger $every6h `
    -Description "Job search pipeline: watch job boards and score new postings." -Force | Out-Null

Get-ScheduledTask -TaskName jobsearch-nightly, jobsearch-watch | Format-Table TaskName, State

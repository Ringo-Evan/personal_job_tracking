import sys

from jobsearch.cli import app

# Redirected output (logs, Task Scheduler) uses the Windows ANSI code page, and one odd character in a company
# name or job title used to crash a whole run mid-way. Replace what the console can't show instead.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(errors="replace")

app(prog_name="jobsearch")

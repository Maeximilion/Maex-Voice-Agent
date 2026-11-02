# Keeps one print bridge running. install.ps1 starts this hidden at logon.
# A loop of its own, because the restart setting of Task Scheduler is meant for a task
# that failed, not for a program that started and ended later.

# The default print log is relative to this folder. A task starts in System32, where the
# user cannot write: the bridge would lose its log and could print a slip twice.
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "bridge.log"

while ($true) {
    # Exactly one bridge per operation: a program left by a dead loop goes first.
    Get-Process maex-printbridge -ErrorAction SilentlyContinue | Stop-Process -Force
    # shortcut: the log is only cut here, at a restart or logon. An outage writes about 5 MB
    # a day until then; give the bridge its own rotation if a PC stays signed in for months.
    Move-Item -Force -ErrorAction SilentlyContinue $log "$log.old"
    Start-Process (Join-Path $PSScriptRoot "maex-printbridge.exe") -NoNewWindow -Wait -RedirectStandardError $log
    Start-Sleep -Seconds 10
}

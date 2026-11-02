# Runs install.ps1 and run.ps1 on a real Windows machine: the runner of printbridge.yml (T-9.6).
# It sets and deletes the bridge settings of the user, so it refuses to run anywhere else.
param([Parameter(Mandatory = $true)][string]$Package)
$ErrorActionPreference = "Stop"
if (-not $env:GITHUB_ACTIONS) { throw "CI only: this would delete the settings of the bridge." }

$installer = Join-Path $Package "install.ps1"
$folder = Join-Path $env:RUNNER_TEMP "MaexPrintBridge"
$log = Join-Path $folder "bridge.log"
$token = "smoke-token-must-not-leak"
$settings = @{
    MAEX_SERVER_URL    = "https://127.0.0.1:9"
    MAEX_KITCHEN_TOKEN = $token
    MAEX_TENANT_ID     = "00000000-0000-0000-0000-000000000000"
    MAEX_PRINTER       = "tcp:127.0.0.1:9"
}

function Assert-That($ok, $what) {
    if (-not $ok) { throw "FAILED: $what" }
    Write-Output "ok: $what"
}

function Wait-Until([scriptblock]$condition, $what) {
    foreach ($try in 1..60) {
        if (& $condition) { Write-Output "ok: $what"; return }
        Start-Sleep -Seconds 1
    }
    throw "FAILED after 60 s: $what"
}

function Invoke-Installer($arguments) {
    # Write-Host: anything left in the output stream would become part of the return value.
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -Folder $folder @arguments | Write-Host
    return $LASTEXITCODE
}

function Get-Bridge { @(Get-Process maex-printbridge -ErrorAction SilentlyContinue) }

Assert-That ((Invoke-Installer @()) -ne 0) "install refuses without settings"
Assert-That (-not (Test-Path $folder)) "a refused install leaves no folder"

foreach ($name in $settings.Keys) {
    [Environment]::SetEnvironmentVariable($name, $settings[$name], "User")
}
Assert-That ((Invoke-Installer @()) -eq 0) "install succeeds with settings"

$task = Get-ScheduledTask -TaskName "Maex Print Bridge"
$xml = Export-ScheduledTask -TaskName "Maex Print Bridge"
Assert-That ($xml -notmatch $token) "the task holds no token"
Assert-That ($xml -match "LogonTrigger") "the task starts at logon"
Assert-That ($task.Settings.ExecutionTimeLimit -eq "PT0S") "the task has no time limit"
Assert-That ($task.Actions[0].WorkingDirectory -eq $folder) "the task starts in the install folder"

Start-Sleep -Seconds 5
Get-ScheduledTaskInfo -TaskName "Maex Print Bridge" | Format-List LastRunTime, LastTaskResult
$task.State
Wait-Until { (Test-Path $log) -and ((Get-Content -Raw $log) -match "Durchlauf abgebrochen") } "the hidden bridge runs and logs"

$first = (Get-Bridge).Id
Get-Bridge | Stop-Process -Force
Wait-Until { @(Get-Bridge | Where-Object { $first -notcontains $_.Id }).Count -gt 0 } "run.ps1 starts the program again"

Assert-That ((Invoke-Installer @("-Uninstall")) -eq 0) "uninstall succeeds"
Assert-That (-not (Get-ScheduledTask -TaskName "Maex Print Bridge" -ErrorAction SilentlyContinue)) "the task is gone"
Assert-That (-not (Test-Path $folder)) "the folder is gone"
Assert-That ((Get-Bridge).Count -eq 0) "the program is stopped"
Assert-That (-not [Environment]::GetEnvironmentVariable("MAEX_KITCHEN_TOKEN", "User")) "the token is gone"

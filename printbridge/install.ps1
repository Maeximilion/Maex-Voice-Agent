<#
Installs the print bridge program for the signed-in Windows user, or removes it.

    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall

Run it again from a newer package to update: the print log stays. Python is not needed.
The settings, the token included, stay in the environment variables of printbridge/README.md:
this script only checks that they exist and writes none of them to the task or to a file.
#>
param(
    [switch]$Uninstall,
    [string]$Folder = (Join-Path $env:LOCALAPPDATA "MaexPrintBridge")
)
$ErrorActionPreference = "Stop"
$TaskName = "Maex Print Bridge"
$Required = "MAEX_SERVER_URL", "MAEX_KITCHEN_TOKEN", "MAEX_TENANT_ID", "MAEX_PRINTER"
$Optional = "MAEX_PRINTER_WIDTH", "MAEX_STATE_FILE", "MAEX_INTERVAL_SECONDS"

$run = Join-Path $Folder "run.ps1"
$program = Join-Path $PSScriptRoot "maex-printbridge.exe"

# Checked before anything stops: a refused update must leave the running bridge alone.
if (-not $Uninstall) {
    # Read from the stored scopes: a value set only in this console never reaches the task.
    $missing = $Required | Where-Object {
        -not ([Environment]::GetEnvironmentVariable($_, "User") -or
            [Environment]::GetEnvironmentVariable($_, "Machine"))
    }
    if ($missing) {
        throw "Settings missing: $($missing -join ', '). Set them with setx first (printbridge/README.md)."
    }
    if (-not (Test-Path $program)) { throw "$program not found: unpack the whole package." }
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) { Stop-ScheduledTask -TaskName $TaskName }
# Stop-ScheduledTask does not wait for run.ps1. Alive, it restarts the program during an
# update and holds the folder during a removal, so it goes first and for certain.
$loops = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($run) }
$running = @($loops | ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
if ($running) {
    $running | Stop-Process -Force
    $running | Wait-Process
}
$running = Get-Process maex-printbridge -ErrorAction SilentlyContinue
if ($running) {
    $running | Stop-Process -Force
    $running | Wait-Process
}

if ($Uninstall) {
    if ($task) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    if (Test-Path $Folder) { Remove-Item -Recurse -Force $Folder }
    # No token stays behind on a machine that no longer runs the bridge. The install
    # check accepts the Machine scope too, so it is cleared as well; that needs an
    # elevated console.
    $left = @()
    foreach ($name in $Required + $Optional) {
        [Environment]::SetEnvironmentVariable($name, $null, "User")
        if ([Environment]::GetEnvironmentVariable($name, "Machine")) {
            try { [Environment]::SetEnvironmentVariable($name, $null, "Machine") }
            catch { $left += $name }
        }
    }
    Write-Output "Print bridge removed."
    if ($left) {
        Write-Warning ("Still set for all users of this PC: $($left -join ', '). " +
            "Run the removal again in a console opened as administrator to clear them.")
    }
    return
}

try {
    New-Item -ItemType Directory -Force $Folder | Out-Null
    Copy-Item $program, (Join-Path $PSScriptRoot "run.ps1") $Folder -Force
    # A downloaded file carries a mark that makes Windows ask first. Nobody sees a hidden start.
    Unblock-File (Join-Path $Folder "maex-printbridge.exe"), (Join-Path $Folder "run.ps1")

    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Folder `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    # Zero means no limit: the default stops a task after three days. The defaults would also
    # stop it on battery. run.ps1 restarts the program; these restart run.ps1.
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
} catch {
    # A failed update must not leave the kitchen without slips: the old bridge runs on.
    if ($task) { Start-ScheduledTask -TaskName $TaskName }
    throw
}
Start-ScheduledTask -TaskName $TaskName
Write-Output "Print bridge installed in $Folder and started. Log: $Folder\bridge.log"

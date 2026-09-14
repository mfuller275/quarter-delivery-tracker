<#
Deploys the Quarter Delivery Tracker on a Windows server (e.g. in-pxl-gdstest).

USAGE (run in an ELEVATED PowerShell so the firewall rule can be created):
    1. Copy this whole project folder to the server, e.g. C:\Apps\quarter-delivery-tracker
    2. Right-click PowerShell > Run as administrator
    3. cd C:\Apps\quarter-delivery-tracker
    4. .\deploy-setup.ps1

What it does (idempotent - safe to re-run):
    - Verifies Python 3.11+ is installed.
    - Creates .venv and installs requirements.txt.
    - Creates .env from .env.example if one isn't present.
    - Registers the "QuarterDeliveryTracker" scheduled task (starts at your logon).
    - Adds the inbound firewall rule for TCP 8000 (Domain + Private).
    - Starts the server now.

After it finishes, sign in to Azure DevOps ONCE: browse to the URL it prints and
click Refresh - a browser MFA popup appears on THIS server; complete it. That token
is cached for the logged-in user so the scheduled task can run unattended.
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    # Bind address: 0.0.0.0 = shareable on the LAN; 127.0.0.1 = local only.
    [string]$BindHost = "0.0.0.0"
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location -Path $root
Write-Host "Deploying from: $root" -ForegroundColor Cyan

# --- 1. Python ---------------------------------------------------------------
# Find a REAL Python 3.11+ interpreter. On modern Windows, "python" may be the
# Python Install Manager / Store stub (no runtime), so we probe several launchers
# and resolve the actual sys.executable. stderr is ignored so the stub's chatter
# doesn't abort the script.
function Get-PythonExe {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    try {
        $candidates = @(
            @("py","-3.12"), @("py","-3.13"), @("py","-3.11"), @("py","-3"),
            @("python"), @("python3")
        )
        foreach ($c in $candidates) {
            $cmd = $c[0]
            $pre = @()
            if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
            if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
            try {
                $exe = & $cmd @pre -c "import sys; sys.stdout.write(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) {
                    $ver = & $exe -c "import sys; sys.stdout.write('%d.%d' % sys.version_info[:2])" 2>$null
                    if ($ver -match "^3\.(1[1-9]|[2-9]\d)$") { return $exe }
                }
            } catch { }
        }
    } finally { $ErrorActionPreference = $prev }
    return $null
}

$pyExe = Get-PythonExe
if (-not $pyExe) {
    throw @"
No real Python 3.11+ interpreter found (only the Python Install Manager stub is present).
Install an actual runtime, then re-run this script. Easiest options:
  * Run:  py install 3.12      (uses the Python Install Manager already on this box)
  * OR download the classic installer 'Windows installer (64-bit)' from
    https://www.python.org/downloads/  and tick 'Add python.exe to PATH'.
"@
}
Write-Host "Python OK: $pyExe" -ForegroundColor Green

# --- 2. venv + dependencies --------------------------------------------------
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    & $pyExe -m venv "$root\.venv"
}
$venvPy = "$root\.venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip | Out-Null
& $venvPy -m pip install -r "$root\requirements.txt"
Write-Host "Dependencies installed." -ForegroundColor Green

# --- 3. .env -----------------------------------------------------------------
if (-not (Test-Path "$root\.env")) {
    if (Test-Path "$root\.env.example") {
        Copy-Item "$root\.env.example" "$root\.env"
        Write-Host "Created .env from .env.example - review it before loading data." -ForegroundColor Yellow
    } else {
        Write-Host "WARNING: no .env or .env.example found." -ForegroundColor Yellow
    }
}

# --- 4. write start-server.ps1 host/port and register scheduled task ---------
# start-server.ps1 ships alongside this script and uses $PSScriptRoot, so it is
# already path-independent. We only ensure the host/port match the requested
# values by (re)writing them here.
$startScript = "$root\start-server.ps1"
if (-not (Test-Path $startScript)) {
    throw "start-server.ps1 is missing from $root. Copy the full project folder."
}
$whoami = whoami
Write-Host "Registering scheduled task 'QuarterDeliveryTracker' for $whoami..." -ForegroundColor Cyan
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $whoami
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $whoami -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "QuarterDeliveryTracker" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal `
    -Description "Starts the Quarter Delivery Tracker dashboard (uvicorn on port $Port) at logon." `
    -Force | Out-Null
Write-Host "Scheduled task registered." -ForegroundColor Green

# --- 5. firewall rule (needs elevation) --------------------------------------
if ($BindHost -ne "127.0.0.1") {
    $ruleName = "Quarter Delivery Tracker (TCP $Port)"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        try {
            New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort $Port -Profile Domain,Private -ErrorAction Stop | Out-Null
            Write-Host "Firewall rule created." -ForegroundColor Green
        } catch {
            Write-Host "Could not create firewall rule (run this script as administrator): $($_.Exception.Message)" -ForegroundColor Yellow
        }
    } else {
        Write-Host "Firewall rule already present." -ForegroundColor Green
    }
}

# --- 6. start now ------------------------------------------------------------
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Stop-Process -Id $listening.OwningProcess -Force -ErrorAction SilentlyContinue
}
Start-Process -FilePath $venvPy `
    -ArgumentList "-m","uvicorn","backend.main:app","--host",$BindHost,"--port","$Port" `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput "$root\server.log" -RedirectStandardError "$root\server.err.log"
Start-Sleep -Seconds 6

try {
    $c = Invoke-RestMethod "http://127.0.0.1:$Port/api/config" -TimeoutSec 10
    Write-Host "Server is up (org=$($c.org))." -ForegroundColor Green
} catch {
    Write-Host "Server did not respond yet - check server.err.log." -ForegroundColor Yellow
}

$name = $env:COMPUTERNAME
$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "=== Done. Share these URLs ===" -ForegroundColor Cyan
Write-Host "  http://${name}:$Port"
if ($ip) { Write-Host "  http://${ip}:$Port" }
Write-Host ""
Write-Host "NEXT: open the URL on this server, click Refresh, and complete the" -ForegroundColor Yellow
Write-Host "one-time Azure DevOps browser sign-in (MFA) to cache the token." -ForegroundColor Yellow

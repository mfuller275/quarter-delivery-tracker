# Requires PowerShell. Creates a virtual environment, installs dependencies,
# and starts the dashboard at http://127.0.0.1:8000
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
}

& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - review it before loading data." -ForegroundColor Yellow
}

Write-Host "Starting dashboard at http://0.0.0.0:8000 (listening on all interfaces)" -ForegroundColor Green
# Ensure a Windows Firewall rule exists to allow inbound TCP 8000
$fwName = 'Quarter Delivery Tracker (TCP 8000)'
try {
    $existing = Get-NetFirewallRule -DisplayName $fwName -ErrorAction SilentlyContinue
} catch {
    $existing = $null
}
if (-not $existing) {
    if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")) {
        Write-Host "Firewall rule $fwName is missing. Run this script as Administrator to create it, or create the rule manually:" -ForegroundColor Yellow
        Write-Host "New-NetFirewallRule -DisplayName '$fwName' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Domain,Private -Description 'Allow Quarter Delivery Tracker web UI'" -ForegroundColor Cyan
    } else {
        Write-Host "Creating firewall rule: $fwName" -ForegroundColor Cyan
        New-NetFirewallRule -DisplayName $fwName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Domain,Private -Description 'Allow Quarter Delivery Tracker web UI' | Out-Null
    }
} else {
    Write-Host "Firewall rule $fwName already exists." -ForegroundColor Green
}

Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList "-m uvicorn backend.main:app --host 0.0.0.0 --port 8000" -WindowStyle Hidden

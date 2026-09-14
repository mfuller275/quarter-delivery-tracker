# Starts the Quarter Delivery Tracker if it isn't already listening on port 8000.
# Launched at logon by the "QuarterDeliveryTracker" scheduled task.
$ErrorActionPreference = "SilentlyContinue"
$root = $PSScriptRoot
Set-Location -Path $root

$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listening) { exit 0 }

# Use python.exe (not pythonw) so uvicorn's logging has a valid stream; the
# window is hidden and stdout/stderr are redirected to log files.
# Bound to 0.0.0.0 to share on the LAN (inbound firewall rule for TCP 8000 is
# created by the "QuarterDeliveryTracker" firewall rule).
Start-Process -FilePath "$root\.venv\Scripts\python.exe" `
  -ArgumentList "-m","uvicorn","backend.main:app","--host","0.0.0.0","--port","8000" `
  -WorkingDirectory $root `
  -WindowStyle Hidden `
  -RedirectStandardOutput "$root\server.log" `
  -RedirectStandardError "$root\server.err.log"

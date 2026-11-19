# stop.ps1 - Cleanly stops all PeakCut and Cloudflare services
$ErrorActionPreference = "SilentlyContinue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "`nStopping PeakCut Studio & Cloudflare Tunnel..." -ForegroundColor Yellow

# Kill process on port 8765
$portProc = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $portProc) {
    if ($p -gt 0) {
        Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        Write-Host "  Stopped PeakCut Server (PID: $p)" -ForegroundColor Green
    }
}

# Kill cloudflared
$cfProcs = Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue
foreach ($p in $cfProcs) {
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    Write-Host "  Stopped Cloudflare Tunnel (PID: $($p.Id))" -ForegroundColor Green
}

Write-Host "Done! All PeakCut services have been stopped.`n" -ForegroundColor Green

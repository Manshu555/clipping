# start.ps1 - Starts PeakCut Server + Cloudflare Tunnel
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

Write-Host "`n===============================================" -ForegroundColor Cyan
Write-Host "         Starting PeakCut Studio & Tunnel        " -ForegroundColor Cyan
Write-Host "===============================================`n" -ForegroundColor Cyan

# 1. Clean up any existing instances on port 8765 or old cloudflared
$oldPort = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($pid_ in $oldPort) {
    if ($pid_ -gt 0) { Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue }
}
Stop-Process -Name "cloudflared" -Force -ErrorAction SilentlyContinue

# 2. Start Cloudflare Tunnel
$logFile = Join-Path $Root "tunnel.log"
if (Test-Path $logFile) { Remove-Item $logFile -Force -ErrorAction SilentlyContinue }

Write-Host "[1/2] Launching Cloudflare Tunnel..." -ForegroundColor Yellow
$cfProc = Start-Process -FilePath "cloudflared" -ArgumentList "tunnel --url http://127.0.0.1:8765 --logfile `"$logFile`"" -PassThru -NoNewWindow

$tunnelUrl = ""
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    if (Test-Path $logFile) {
        $content = Get-Content $logFile -Raw -ErrorAction SilentlyContinue
        if ($content -match '(https://[a-zA-Z0-9\-]+\.trycloudflare\.com)') {
            $tunnelUrl = $matches[1]
            break
        }
    }
}

if ($tunnelUrl) {
    Write-Host "  Public Tunnel URL: $tunnelUrl" -ForegroundColor Green
    # Update .env with current public URL
    $envFile = Join-Path $Root ".env"
    if (Test-Path $envFile) {
        $envText = Get-Content $envFile -Raw -Encoding utf8
        if ($envText -match 'PEAKCUT_PUBLIC_URL=.*') {
            $envText = $envText -replace 'PEAKCUT_PUBLIC_URL=.*', "PEAKCUT_PUBLIC_URL=$tunnelUrl"
        } else {
            $envText += "`nPEAKCUT_PUBLIC_URL=$tunnelUrl"
        }
        [System.IO.File]::WriteAllText($envFile, $envText, [System.Text.Encoding]::UTF8)
    }
} else {
    Write-Host "  Warning: Could not detect trycloudflare URL in 15s. Using existing .env setting." -ForegroundColor DarkYellow
}

# 3. Start PeakCut Server
Write-Host "[2/2] Launching PeakCut Web Server..." -ForegroundColor Yellow
Write-Host "`n  Local Dashboard:  http://127.0.0.1:8765/app" -ForegroundColor Cyan
if ($tunnelUrl) {
    Write-Host "  Public Address:   $tunnelUrl/app" -ForegroundColor Cyan
}
Write-Host "`nPress Ctrl+C to stop both services, or run .\stop.bat / .\stop.ps1`n" -ForegroundColor DarkGray

try {
    & py -3.12 -m studio.server
} finally {
    Write-Host "`nStopping services..." -ForegroundColor Yellow
    if ($cfProc -and -not $cfProc.HasExited) {
        Stop-Process -Id $cfProc.Id -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Name "cloudflared" -Force -ErrorAction SilentlyContinue
    Write-Host "All PeakCut services stopped.`n" -ForegroundColor Green
}

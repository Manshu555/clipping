<#
.SYNOPSIS
    Record a Twitch channel with PeakCut and clip every chat spike.

.DESCRIPTION
    .\watch.ps1 <twitch_channel> --min 5

    Checks that Python, ffmpeg, ffprobe and PeakCut's packages are here (and installs missing Python packages),
    then hands over to "python -m studio.watch", which drives the PeakCut on this computer: the clips show up in
    the dashboard like any other. Ctrl+C stops the recording and finishes the clips already found.

    Everything after the channel name goes to the watcher as it is:
      --min N                 fewest chat messages in 10 seconds that can count as a spike (default 5)
      --no-keep-recording     keep only the rolling buffer, not the whole stream in rec/
      --url http://...        where PeakCut runs (default http://127.0.0.1:8765)
#>
# Continue, not Stop: Windows PowerShell 5.1 turns any line a program writes to stderr into an error under Stop.
# Every step below checks the program's exit code instead.
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot

function Say($msg) { Write-Host $msg }
function Fail($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }

if ($args.Count -lt 1 -or $args[0] -in @("-h", "--help", "/?")) {
    Say "Usage: .\watch.ps1 <twitch_channel> [--min 5] [--no-keep-recording]"
    Say "Example: .\watch.ps1 shroud --min 8"
    exit 0
}

# ---- Python 3.10 or newer
$py = $null
foreach ($cand in @("python", "py")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $v = & $cand -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $parts = $v.Trim().Split(".")
                if ([int]$parts[0] -gt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10)) { $py = $cand; break }
            }
        } catch { }
    }
}
if (-not $py) { Fail "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ (tick 'Add to PATH')." }
Say "Python: $(& $py --version)"

# ---- ffmpeg and ffprobe (PeakCut also looks in the usual install folders, so a fresh winget install is found)
$found = & $py -c "from studio import tools; print(','.join(tools.setup()))" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say "PeakCut's packages are not installed yet. Installing them (once)..."
    & $py -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Fail "Installing PeakCut's packages failed. Run: $py -m pip install -r requirements.txt" }
    $found = & $py -c "from studio import tools; print(','.join(tools.setup()))"
}
if ($found -and $found.Trim()) {
    Fail ("Missing: " + $found.Trim() + ". Install ffmpeg (it includes ffprobe) with: winget install Gyan.FFmpeg  then open a new window and run this again.")
}
Say "ffmpeg and ffprobe: found"

# ---- PeakCut's packages, and the Twitch extras (Streamlink, Whisper)
& $py -c "import fastapi, uvicorn, numpy, sherpa_onnx, yt_dlp, soundfile, PIL, httpx" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say "Some of PeakCut's packages are missing. Installing them..."
    & $py -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Fail "Installing PeakCut's packages failed. Run: $py -m pip install -r requirements.txt" }
}
& $py -c "import streamlink, whisper" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say "Installing the Twitch extras (Streamlink, and Whisper for the .srt subtitles)..."
    # --no-cache-dir: pip's cache can fail to build Whisper on Windows ("cannot move the file to a different disk drive")
    & $py -m pip install --no-cache-dir -r requirements-twitch.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "The Twitch extras did not install. Carrying on: recording uses yt-dlp, and subtitles use PeakCut's own transcript." -ForegroundColor Yellow
    }
}

# ---- hand over. Ctrl+C reaches the watcher, which stops the recording and finishes the clips in progress.
& $py -m studio.watch @args
exit $LASTEXITCODE

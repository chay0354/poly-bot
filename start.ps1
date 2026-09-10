# Run the bot locally on Windows and keep it running.
#
#   .\start.ps1            live/paper per .env
#   .\start.ps1 -Paper     force paper mode (no orders, no money)
#
# Restarts the bot if it crashes; Ctrl+C stops it for good. data/bot.lock
# stops a second copy from starting on this machine, but NOT a copy on
# Railway — stop that service first or both will place orders.

param([switch]$Paper)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# The logs use Δ ≥ ── etc.; without UTF-8 the console prints '?' or throws.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

if (-not (Test-Path ".env")) {
    Write-Error "No .env found. Copy .env.example to .env and fill it in."
}

$py = ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating .venv and installing requirements..."
    python -m venv .venv
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet -r requirements.txt
}

if ($Paper) { $env:PM_MODE = "paper" }

$mode = if ($env:PM_MODE) { $env:PM_MODE } else { (Select-String -Path .env -Pattern '^PM_MODE=(\w+)').Matches[0].Groups[1].Value }
Write-Host "Starting bot in $mode mode (Ctrl+C to stop)."
if ($mode -eq "live") {
    Write-Host "LIVE: make sure the Railway service is stopped, or two bots will trade." -ForegroundColor Yellow
}

while ($true) {
    & $py run.py
    $code = $LASTEXITCODE
    if ($code -eq 0) { break }        # clean exit (Ctrl+C is handled inside run.py)
    Write-Host "bot exited with code $code; restarting in 5s (Ctrl+C to stop)" -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}

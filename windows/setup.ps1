# One-time setup on Windows. Run from the repo folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File windows\setup.ps1
# Installs uv, a Java 25 runtime and signal-cli 0.14.9, then the Python environment.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$SignalVersion = "0.14.9"
$SignalDir = Join-Path $env:LOCALAPPDATA "signal-cli"

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv..."
    winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    Refresh-Path
}

if (-not (Get-Command java -ErrorAction SilentlyContinue)) {
    Write-Host "Installing Java 25 (Temurin JRE)..."
    winget install --id EclipseAdoptium.Temurin.25.JRE -e --accept-source-agreements --accept-package-agreements
    Refresh-Path
}

$SignalBat = Join-Path $SignalDir "signal-cli-$SignalVersion\bin\signal-cli.bat"
if (-not (Test-Path $SignalBat)) {
    Write-Host "Installing signal-cli $SignalVersion..."
    New-Item -ItemType Directory -Force $SignalDir | Out-Null
    $Archive = Join-Path $env:TEMP "signal-cli-$SignalVersion.tar.gz"
    Invoke-WebRequest "https://github.com/AsamK/signal-cli/releases/download/v$SignalVersion/signal-cli-$SignalVersion.tar.gz" -OutFile $Archive
    tar -xzf $Archive -C $SignalDir
    Remove-Item $Archive
}
& $SignalBat --version

Write-Host "Installing Python dependencies..."
Push-Location $Root
uv sync
Pop-Location

Write-Host ""
Write-Host "Done. Put this line in .env:"
Write-Host "SIGNAL_CLI_PATH=$SignalBat"

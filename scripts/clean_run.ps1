param(
    [int]$Port = 9222
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Output "Stopping src.main Python processes..."
$pythonMatches = @(
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'src\.main' }
)
foreach ($proc in $pythonMatches) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

Write-Output "Stopping dedicated CDP Chrome (CDP_Profile)..."
$chromeMatches = @(
    Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'CDP_Profile' }
)
foreach ($proc in $chromeMatches) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1

$freePort = Join-Path $PSScriptRoot "free_cdp_port.ps1"
if (Test-Path $freePort) {
    & $freePort -Port $Port
}

Write-Output "Processes stopped. Waiting before SQLite reset..."
